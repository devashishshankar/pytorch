# Owner(s): ["oncall: quantization"]
import torch
import torch.nn as nn
import torch._dynamo as torchdynamo
from torch.testing._internal.common_utils import xfailIfPython311
from torch.testing._internal.common_quantization import (
    QuantizationTestCase,
    skip_if_no_torchvision,
    skipIfNoQNNPACK,
)
from torch.testing._internal.common_quantization import NodeSpec as ns
from torch.testing._internal.common_quantized import (
    override_quantized_engine,
)
from torch.ao.quantization import (
    get_default_qconfig,
    QConfigMapping,
    observer,
)
from torch.ao.quantization.backend_config import (
    get_qnnpack_backend_config,
)
from torch.ao.quantization.backend_config._qnnpack_pt2e import get_qnnpack_pt2e_backend_config
from torch.ao.quantization.quantize_fx import prepare_fx, convert_to_reference_fx
from torch.ao.quantization._quantize_pt2e import prepare_pt2e, convert_pt2e
from torch.ao.ns.fx.utils import (
    compute_sqnr,
)
import copy
import itertools

from torch._decomp import get_decompositions
from torch.fx.experimental.proxy_tensor import make_fx

quant_decomp = get_decompositions(
    [
        torch.ops.quantized_decomposed.quantize_per_tensor,
        torch.ops.quantized_decomposed.quantize_per_tensor.tensor,
        torch.ops.quantized_decomposed.dequantize_per_tensor,
        torch.ops.quantized_decomposed.dequantize_per_tensor.tensor,
    ]
)

@skipIfNoQNNPACK
class TestQuantizePT2E(QuantizationTestCase):
    @xfailIfPython311
    def test_qconfig_none(self):
        class M(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.conv1 = nn.Conv2d(1, 1, 1)
                self.conv2 = nn.Conv2d(1, 1, 1)

            def forward(self, x):
                x = self.conv1(x)
                x = self.conv2(x)
                return x

        with override_quantized_engine("qnnpack"):
            m = M().eval()
            example_inputs = (torch.randn(1, 1, 1, 1),)
            # program capture
            m, guards = torchdynamo.export(
                m,
                *copy.deepcopy(example_inputs),
                aten_graph=True,
                tracing_mode="real",
            )

            qconfig = get_default_qconfig("qnnpack")
            qconfig_mapping = QConfigMapping().set_global(qconfig) \
                                              .set_module_name("conv2", None)
            backend_config = get_qnnpack_pt2e_backend_config()
            m = prepare_pt2e(m, qconfig_mapping, example_inputs, backend_config)
            m(*example_inputs)
            m = convert_pt2e(m)
            m(*example_inputs)

            # first conv is quantized, second conv is not quantized
            node_occurrence = {
                # two for input of the first conv, one for output for the first conv
                ns.call_function(torch.ops.quantized_decomposed.quantize_per_tensor): 3,
                ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor): 3,
            }
            node_list = [
                ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor),
                ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor),
                ns.call_function(torch.ops.aten.convolution.default),
                ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor),
                ns.call_function(torch.ops.aten.convolution.default),
            ]
            self.checkGraphModuleNodes(
                m, expected_node_list=node_list, expected_node_occurrence=node_occurrence)

    @xfailIfPython311
    def test_qconfig_module_type(self):
        class M(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.conv = nn.Conv2d(1, 1, 1)
                self.linear = nn.Linear(9, 3)

            def forward(self, x):
                x = self.conv(x)
                x = x.reshape((1, -1))
                x = self.linear(x)
                return x

        with override_quantized_engine("qnnpack"):
            m = M().eval()
            example_inputs = (torch.randn(1, 1, 3, 3),)

            # program capture
            m, guards = torchdynamo.export(
                m,
                *copy.deepcopy(example_inputs),
                aten_graph=True,
                tracing_mode="real",
            )

            qconfig = get_default_qconfig("qnnpack")
            qconfig_mapping = QConfigMapping().set_object_type(torch.nn.Conv2d, qconfig)
            backend_config = get_qnnpack_pt2e_backend_config()
            m = prepare_pt2e(m, qconfig_mapping, example_inputs, backend_config)
            m(*example_inputs)
            m = convert_pt2e(m)
            m(*example_inputs)
            # conv is quantized, linear is not quantized
            node_occurrence = {
                # two for input and weight of the conv, one for output for the conv
                ns.call_function(torch.ops.quantized_decomposed.quantize_per_tensor): 3,
                ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor): 3,
            }
            node_list = [
                ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor),
                ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor),
                ns.call_function(torch.ops.aten.convolution.default),
                ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor),
                ns.call_function(torch.ops.aten.addmm.default),
            ]
            self.checkGraphModuleNodes(
                m,
                expected_node_list=node_list,
                expected_node_occurrence=node_occurrence
            )

    @xfailIfPython311
    def test_q_dq_decomposition(self):
        class M(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.conv = nn.Conv2d(1, 1, 1)

            def forward(self, x):
                x = self.conv(x)
                return x

        with override_quantized_engine("qnnpack"):
            m = M().eval()
            example_inputs = (torch.randn(1, 1, 3, 3),)

            # program capture
            m, guards = torchdynamo.export(
                m,
                *copy.deepcopy(example_inputs),
                aten_graph=True,
                tracing_mode="real",
            )

            qconfig = get_default_qconfig("qnnpack")
            qconfig_mapping = QConfigMapping().set_object_type(torch.nn.Conv2d, qconfig)
            backend_config = get_qnnpack_pt2e_backend_config()
            m = prepare_pt2e(m, qconfig_mapping, example_inputs, backend_config)
            m(*example_inputs)
            m = convert_pt2e(m)
            m(*example_inputs)
            node_occurrence = {
                # two for input and weight of the conv, one for output for the conv
                ns.call_function(torch.ops.quantized_decomposed.quantize_per_tensor): 3,
                ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor): 3,
            }
            node_list = [
                ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor),
                ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor),
                ns.call_function(torch.ops.aten.convolution.default),
                ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor),
            ]
            self.checkGraphModuleNodes(
                m,
                expected_node_list=node_list,
                expected_node_occurrence=node_occurrence
            )
            m = make_fx(m, decomposition_table=quant_decomp)(*copy.deepcopy(example_inputs))
            node_occurrence = {
                # check both q/dq are decomposed
                ns.call_function(torch.ops.quantized_decomposed.quantize_per_tensor.default): 0,
                ns.call_function(torch.ops.quantized_decomposed.dequantize_per_tensor.default): 0,
            }
            node_list = [
                # ops in quantize
                ns.call_function(torch.ops.aten.mul.Tensor),
                ns.call_function(torch.ops.aten.round.default),
                ns.call_function(torch.ops.aten.add.Tensor),
                ns.call_function(torch.ops.aten.clamp.default),
                # ops in dequantize
                ns.call_function(torch.ops.aten.sub.Tensor),
                ns.call_function(torch.ops.aten.mul.Tensor),
                # conv op
                ns.call_function(torch.ops.aten.convolution.default),
            ]
            self.checkGraphModuleNodes(
                m,
                expected_node_list=node_list,
                expected_node_occurrence=node_occurrence
            )
    def test_comment(self):
        from torch._subclasses.fake_tensor import (
            FakeTensor,
            FakeTensorMode,
            FakeTensorConverter,
        )


        class Mod(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                # self.relu = torch.nn.ReLU()

            def forward(self, x):
                # return self.relu(x + x)
                return x + x

        with override_quantized_engine("qnnpack"):
            example_inputs = (torch.randn(1, 3, 224, 224),)
            m = Mod().eval()
            # program capture
            m, guards = torchdynamo.export(
                m,
                *copy.deepcopy(example_inputs),
                aten_graph=True,
                tracing_mode="real",
            )

            backend_config = get_qnnpack_pt2e_backend_config()
            qconfig = get_default_qconfig("qnnpack")
            qconfig_mapping = QConfigMapping().set_global(qconfig)
            before_fusion_result = m(*example_inputs)

            m = prepare_pt2e(m, qconfig_mapping, example_inputs, backend_config)
            m(*example_inputs)
            m = convert_pt2e(m)

            # Way 1 to trigger the issue
            with FakeTensorMode(allow_non_fake_inputs=True) as mode:
                fake_x = mode.from_tensor(torch.rand((1, 3, 224, 224)).to(memory_format=torch.channels_last))
                out = m(fake_x)

            # Way 2 to trigger the issue
            # run = torch._dynamo.optimize(compile_fx, nopython=False)(m)
            # inductor_result = run(*example_inputs)

        @Xfailifpython311
        def test_rearrange_weight_observer_for_decomposed_linear(self):
            """
            Check whether weight observer is correctly rearranged for decomposed linear.
            before:
                weight - t - observer \
                  input - observer - addmm/mm
            after:
                weight - observer - t \
                  input - observer - addmm/mm
            """
            class M(torch.nn.Module):
                def __init__(self, with_bias, use_relu):
                    super().__init__()
                    self.linear = nn.Linear(4, 4, bias=with_bias)
                    self.relu = nn.ReLU()
                    self.use_relu = use_relu

                def forward(self, x):
                    x = self.linear(x)
                    return self.relu(x) if self.use_relu else x

            with_bias_list = [True, False]
            use_relu_list = [True, False]
            cases = itertools.product(with_bias_list, use_relu_list)
            for with_bias, use_relu in cases:
                m = M(with_bias, use_relu).eval()
                example_inputs = (torch.randn(1, 4),)

                # program capture
                m, guards = torchdynamo.export(
                    m,
                    *copy.deepcopy(example_inputs),
                    aten_graph=True,
                    tracing_mode="real",
                )

                qconfig = get_default_qconfig('qnnpack')
                qconfig_mapping = QConfigMapping().set_global(qconfig)
                backend_config = get_qnnpack_pt2e_backend_config()
                m = prepare_pt2e(m, qconfig_mapping, example_inputs, backend_config)

                # 1. Check graph nodes:
                # - args[0] of t should be the weight observer
                # - args[-1] of addmm/mm should be t
                error_msg = 'Weight observer is not correctly rearranged for decomposed linear'
                for node in m.graph.nodes:
                    if node.target == torch.ops.aten.t.default:
                        target = node.args[0].target
                        self.assertTrue(isinstance(getattr(m, target), observer.ObserverBase), error_msg)
                    elif node.target in (torch.ops.aten.addmm.default, torch.ops.aten.mm.default):
                        target = node.args[-1].target
                        self.assertTrue(target == torch.ops.aten.t.default, error_msg)

                # 2. Check m.code to ensure `m.recompile()` is called.
                # If weight observer is rearranged in graph but `m.recompile()` is not called,
                # m.code would be wrong.
                code_before_recompile = m.code
                m.recompile()
                code_after_recompile = m.code
                self.assertTrue(code_before_recompile == code_after_recompile, error_msg)

class TestQuantizePT2EModels(QuantizationTestCase):
    @skip_if_no_torchvision
    @skipIfNoQNNPACK
    @xfailIfPython311
    def test_resnet18(self):
        import torchvision
        with override_quantized_engine("qnnpack"):
            example_inputs = (torch.randn(1, 3, 224, 224),)
            m = torchvision.models.resnet18().eval()
            m_copy = copy.deepcopy(m)
            # program capture
            m, guards = torchdynamo.export(
                m,
                *copy.deepcopy(example_inputs),
                aten_graph=True,
                tracing_mode="real",
            )

            backend_config = get_qnnpack_pt2e_backend_config()
            # TODO: define qconfig_mapping specifically for executorch
            qconfig = get_default_qconfig("qnnpack")
            qconfig_mapping = QConfigMapping().set_global(qconfig)
            before_fusion_result = m(*example_inputs)

            m = prepare_pt2e(m, qconfig_mapping, example_inputs, backend_config)

            # checking that we inserted observers correctly for maxpool operator (input and
            # output share observer instance)
            self.assertEqual(id(m.activation_post_process_3), id(m.activation_post_process_2))
            after_prepare_result = m(*example_inputs)
            m = convert_pt2e(m)

            after_quant_result = m(*example_inputs)

            # comparing with existing fx graph mode quantization reference flow
            backend_config = get_qnnpack_backend_config()
            m_fx = prepare_fx(m_copy, qconfig_mapping, example_inputs, backend_config=backend_config)
            after_prepare_result_fx = m_fx(*example_inputs)
            m_fx = convert_to_reference_fx(m_fx, backend_config=backend_config)

            after_quant_result_fx = m_fx(*example_inputs)

            # the result matches exactly after prepare
            self.assertEqual(after_prepare_result, after_prepare_result_fx)
            self.assertEqual(compute_sqnr(after_prepare_result, after_prepare_result_fx), torch.tensor(float("inf")))
            # there are slight differences after convert due to different implementations
            # of quant/dequant
            self.assertTrue(torch.max(after_quant_result - after_quant_result_fx) < 1e-1)
            self.assertTrue(compute_sqnr(after_quant_result, after_quant_result_fx) > 35)
