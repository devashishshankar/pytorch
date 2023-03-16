# Owner(s): ["module: functorch"]
import unittest

import torch
from functorch.experimental import control_flow
from functorch.experimental.control_flow import cond
from functorch.experimental.control_flow import UnsupportedAliasMutationException
from torch.fx.experimental.proxy_tensor import make_fx

from torch.testing._internal.common_utils import run_tests, TestCase

def op_count(op, gm):
    count = 0
    for mod in gm.children():
        count += op_count(op, mod)
    for node in gm.graph.nodes:
        if node.target == op:
            count += 1
    return count

class TestControlFlow(TestCase):
    def test_cond_no_trace(self):
        def true_fn(x):
            return x.sin()

        def false_fn(x):
            return x.cos()

        x = torch.randn(4)
        result = cond(False, true_fn, false_fn, [x])
        self.assertEqual(result, torch.cos(x))

    @unittest.skipIf(not torch.cuda.is_available(), "Test requires CUDA.")
    def test_cond_gpu(self):
        def true_fn(x):
            return x.sin()

        def false_fn(x):
            return x.cos()

        x = torch.randn(4, device="cuda")
        pred = torch.tensor(False, device="cuda")
        result = cond(False, true_fn, false_fn, [x])
        self.assertEqual(result, torch.cos(x))

    @unittest.skipIf(not torch.cuda.is_available(), "Test requires CUDA.")
    def test_map_gpu(self):
        def f(x, y):
            return x + y

        xs = torch.ones(3, 2, 2, device="cuda")
        y = torch.ones(2, device="cuda")
        res = control_flow.map(f, xs, y)

        self.assertEqual(res, control_flow.map(f, torch.ones(3, 2, 2), torch.ones(2)))

    @unittest.skipIf(not torch.cuda.is_available(), "Test requires CUDA.")
    def test_while_loop_no_trace_gpu(self):
        def cond_fun(iter, val):
            return iter > 0

        def body_fun(iter, val):
            return (iter - 1, val.sin())

        iter = torch.tensor(5)
        val = torch.randn(2, 3, device="cuda")
        res_scalar = control_flow.while_loop(cond_fun, body_fun, (iter, val))
        while iter > 0:
            val = val.sin()
            iter -= 1
        self.assertEqual(res_scalar, (0, val))

    def test_while_loop_no_trace(self):
        def cond_fun(iter, val):
            return iter > 0

        def body_fun(iter, val):
            return (iter - 1, val.sin())

        iter = torch.tensor(5)
        val = torch.randn(2, 3)
        res_scalar = control_flow.while_loop(cond_fun, body_fun, (iter, val))
        while iter > 0:
            val = val.sin()
            iter -= 1
        self.assertEqual(res_scalar, (0, val))

    def test_while_loop_no_trace_nested(self):

        def fun(iter, val):
            return (iter - 1, val + 1)

        def cond_fun(iter, val):
            return iter > 0

        def body_fun(iter, val):
            _, val = control_flow.while_loop(cond_fun, fun, (inner_iter, val))
            return (iter - 1, val)

        iter = torch.tensor(5)
        inner_iter = torch.tensor(2)
        total_iter = iter * inner_iter
        val = torch.randn(2, 3)
        res_scalar = control_flow.while_loop(cond_fun, body_fun, (iter, val))
        while total_iter > 0:
            val = val + 1
            total_iter -= 1
        self.assertEqual(res_scalar, (0, val))

    def test_while_loop_no_trace_functionalize(self):
        def cond_fun(iter, val):
            iter_ = iter.view(1) + 1
            iter_.add_(-1)
            return iter_ > 0

        def body_fun(iter, val):
            val_ = val.view(3, 2) + 2
            val_.add_(-1)
            return (iter - 1, val_.view(2, 3))

        def f(input):
            return control_flow.while_loop(cond_fun, body_fun, input)

        iter = torch.tensor(5)
        val = torch.randn(2, 3)
        input = (iter, val)
        res_eager = f(input)
        ff = torch.func.functionalize(f, remove="mutations")
        ff_no_view = torch.func.functionalize(f, remove="mutations_and_views")
        res_ff = ff(input)
        res_ff_no_view = ff_no_view(input)
        while iter > 0:
            val = val + 1
            iter -= 1
        expected = (iter, val)
        self.assertEqual(res_eager, expected)
        self.assertEqual(res_ff, expected)
        self.assertEqual(res_ff_no_view, expected)

    def test_while_loop_no_trace_nested_functionalize(self):
        def inner_fun(iter, val):
            val_ = val.view(3, 2) + 2
            val_.add_(-1)
            return (iter - 1, val_.view(2, 3))

        def inner_cond_fun(iter, val):
            iter_ = iter.view(1) + 1
            iter_.add_(-1)
            return iter_ > 0

        def outter_cond_fun(iter, inner_iter, val):
            iter_ = iter.view(1) + 1
            iter_.add_(-1)
            return iter_ > 0

        def outter_fun(iter, inner_iter, val):
            _, val = control_flow.while_loop(inner_cond_fun, inner_fun, (inner_iter, val))
            # clone inner_iter to avoid aliasing input
            return (iter - 1, inner_iter.clone(), val)

        iter = torch.tensor(2)
        inner_iter = torch.tensor(5)
        total_iter = iter * inner_iter
        input = (iter, inner_iter, torch.zeros(2, 3))

        def f(input):
            return control_flow.while_loop(outter_cond_fun, outter_fun, input)

        res_eager = f(input)

        ff = torch.func.functionalize(f, remove="mutations")
        ff_no_view = torch.func.functionalize(f, remove="mutations_and_views")
        res_ff = ff(input)
        res_ff_no_view = ff_no_view(input)

        val = input[2]
        while total_iter > 0:
            val = val + 1
            total_iter -= 1

        expected = (0, inner_iter, val)
        self.assertEqual(res_eager, expected)
        self.assertEqual(res_ff, expected)
        self.assertEqual(res_ff_no_view, expected)


class TestControlFlowTraced(TestCase):
    def test_cond_traced_not_nested(self):
        def true_fn(x):
            return x.sin()

        def false_fn(x):
            return x.cos()

        def f(x, y):
            return cond(y, true_fn, false_fn, [x])

        x = torch.randn(4)
        graph = make_fx(f)(x, torch.tensor(False))
        result_true = graph.forward(x, torch.tensor(True))
        result_false = graph.forward(x, torch.tensor(False))
        self.assertFalse(torch.allclose(result_true, result_false))
        self.assertEqual(result_true, torch.sin(x))
        self.assertEqual(result_false, torch.cos(x))

        graph = make_fx(f, tracing_mode="symbolic")(x, torch.tensor(False))
        self.assertEqual(graph(x, torch.tensor(True)), f(x, torch.tensor(True)))

    def test_cond_nested_traced(self):
        def true_nested(y):
            return y * y

        def false_nested(y):
            return y + y

        def true_fn(x, pred2):
            z = cond(pred2, true_nested, false_nested, [x])
            return x + z

        def false_fn(x, _):
            return x.cos()

        def f(x, pred, pred2):
            return cond(pred, true_fn, false_fn, [x, pred2])

        x = torch.randn(4)
        graph = make_fx(f)(x, torch.tensor(False), torch.tensor(False))

        result_true_true = graph.forward(x, torch.tensor(True), torch.tensor(True))  # True + True -> x * x
        result_true_false = graph.forward(x, torch.tensor(True), torch.tensor(False))  # True + True -> x + x
        result_false_true = graph.forward(x, torch.tensor(False), torch.tensor(True))  # False + either -> cos
        result_false_false = graph.forward(x, torch.tensor(False), torch.tensor(False))  # False + either -> cos

        self.assertNotEqual(result_true_true, result_true_false)
        self.assertFalse(torch.allclose(result_false_true, result_true_true))

        self.assertEqual(result_false_true, result_false_false)

        self.assertEqual(result_true_true, (x * x) + x)
        self.assertEqual(result_true_false, x + x + x)

        self.assertEqual(result_false_true, torch.cos(x))

        graph = make_fx(f, tracing_mode="symbolic")(x, torch.tensor(False), torch.tensor(False))
        self.assertEqual(graph(x, torch.tensor(True), torch.tensor(True)), f(x, torch.tensor(True), torch.tensor(True)))

    def test_cond_functionalized(self):
        def true_fn(x):
            y = x.sin()
            y.add_(4)
            return x.sin().max() + y.sum()

        def false_fn(x):
            return x.cos().min()

        def f(x):
            pred = x.shape[0] == 1
            return cond(pred, true_fn, false_fn, [x])

        example_inputs = (torch.ones(4, 5),)
        functional_f = torch.func.functionalize(f)
        self.assertEqual(functional_f(*example_inputs), f(*example_inputs))

        graph_module = make_fx(torch.func.functionalize(f))(*example_inputs)
        self.assertEqual(graph_module(*example_inputs), f(*example_inputs))

        all_ops_in_true_branch = []
        for node in graph_module.true_graph_0.graph.nodes:
            if node.op == "call_function":
                all_ops_in_true_branch.append(node.target)

        self.assertFalse(any([op._schema.is_mutable for op in all_ops_in_true_branch]))

        graph_module = make_fx(torch.func.functionalize(f), tracing_mode="symbolic")(*example_inputs)
        self.assertEqual(graph_module(*example_inputs), f(*example_inputs))

    def test_cond_retrace_functionalized(self):
        def true_fn(x):
            return x.sin()

        def false_fn(x):
            return x.cos()

        def f(x):
            return cond(x.all(), true_fn, false_fn, (x,))

        inp = torch.ones(1, 2)
        gm_non_functional = make_fx(f, tracing_mode="real")(inp)
        gm_functional = make_fx(torch.func.functionalize(gm_non_functional), tracing_mode="real")(inp)
        self.assertEqual(gm_functional(torch.zeros(1, 2)), f(torch.zeros(1, 2)))

    def test_cond_functionalized_nested(self):
        def true_true_fn(x):
            y = x.cos()
            y.add_(4)
            return x.sin().max() + y.sin().max()

        def true_false_fn(x):
            return x.cos().min()

        def true_fn(x):
            pred = x.shape[0] == 1
            return cond(pred, true_true_fn, true_false_fn, [x])

        def false_fn(x):
            return x.sum()

        def f(x):
            pred = x.shape[0] == 1
            return cond(pred, true_fn, false_fn, [x])

        example_inputs = (torch.ones(4, 5),)
        functional_f = torch.func.functionalize(f)
        self.assertEqual(functional_f(*example_inputs), f(*example_inputs))

        graph_module = make_fx(torch.func.functionalize(f))(*example_inputs)
        self.assertEqual(graph_module(*example_inputs), f(*example_inputs))

        gm_true_true_branch = graph_module.true_graph_0.true_graph_0

        graph_module1 = make_fx(torch.func.functionalize(f), tracing_mode="symbolic")(*example_inputs)
        self.assertEqual(graph_module1(*example_inputs), f(*example_inputs))

        all_ops = []
        for node in gm_true_true_branch.graph.nodes:
            if node.op == "call_function":
                all_ops.append(node.target)

        self.assertFalse(any([op._schema.is_mutable for op in all_ops]))

    def test_cond_functionalized_data_dependent_pred(self):
        def true_fn(x):
            return x.sin().sum()

        def false_fn(x):
            return x.cos().sum()

        def f(x):
            pred = x.nonzero().shape[0] == 1
            return cond(pred, true_fn, false_fn, [x])

        example_inputs = (torch.ones(4, 5),)
        functional_f = torch.func.functionalize(f)
        self.assertEqual(functional_f(*example_inputs), f(*example_inputs))

        graph_module = make_fx(torch.func.functionalize(f))(*example_inputs)
        self.assertEqual(graph_module(*example_inputs), f(*example_inputs))

    def test_cond_functionalized_input_mutation_on_true_branch(self):
        def true_fn(x):
            view_x = x.view(x.shape)
            view_x.add_(1)
            return view_x.sin().sum()

        def false_fn(x):
            return x.cos().sum()

        def f(x):
            pred = x.shape[0] == 4
            return cond(pred, true_fn, false_fn, [x])

        example_inputs = (torch.ones(4, 5),)
        functional_f = torch.func.functionalize(f)
        with self.assertRaisesRegex(UnsupportedAliasMutationException, "One of torch.cond branch"):
            functional_f(*example_inputs)

        with self.assertRaisesRegex(UnsupportedAliasMutationException, "One of torch.cond branch"):
            make_fx(torch.func.functionalize(f))(*example_inputs)

    def test_cond_functionalized_input_mutation_on_false_branch(self):
        def true_fn(x):
            return x.sin().sum()

        def false_fn(x):
            view_x = x.view(x.shape)
            view_x.add_(1)
            return view_x.cos().sum()

        def f(x):
            pred = x.shape[0] == 4
            return cond(pred, true_fn, false_fn, [x])

        example_inputs = (torch.ones(5, 5),)
        functional_f = torch.func.functionalize(f)
        with self.assertRaisesRegex(UnsupportedAliasMutationException, "One of torch.cond branch"):
            functional_f(*example_inputs)

        with self.assertRaisesRegex(UnsupportedAliasMutationException, "One of torch.cond branch"):
            make_fx(torch.func.functionalize(f))(*example_inputs)

    def test_cond_functionalized_output_alias_input(self):
        def true_fn(x):
            return x

        def false_fn(x):
            view_x = x.view(x.shape)
            return view_x

        def f(x):
            pred = x.shape[0] == 4
            return cond(pred, true_fn, false_fn, [x])

        example_inputs = (torch.ones(5, 5),)
        functional_f = torch.func.functionalize(f)

        with self.assertRaisesRegex(UnsupportedAliasMutationException, "One of torch.cond branch might be aliasing"):
            functional_f(*example_inputs)

        with self.assertRaisesRegex(UnsupportedAliasMutationException, "One of torch.cond branch might be aliasing"):
            make_fx(torch.func.functionalize(f))(*example_inputs)

    def test_cond_functionalized_nested_input_mutation(self):
        def true_true_fn(x):
            x.add_(4)
            return x.sin().max()

        def true_false_fn(x):
            return x.cos().min()

        def true_fn(x):
            pred = x.shape[0] == 1
            return cond(pred, true_true_fn, true_false_fn, [x])

        def false_fn(x):
            return x.sum()

        def f(x):
            pred = x.shape[0] == 1
            return cond(pred, true_fn, false_fn, [x])

        example_inputs = (torch.ones(4, 5),)
        functional_f = torch.func.functionalize(f)
        with self.assertRaisesRegex(UnsupportedAliasMutationException, "One of torch.cond branch"):
            functional_f(*example_inputs)

        with self.assertRaisesRegex(UnsupportedAliasMutationException, "One of torch.cond branch"):
            make_fx(torch.func.functionalize(f))(*example_inputs)

    def test_cond_nested_traced_other_inputs(self):
        def true_nested(y):
            return y * y

        def false_nested(y):
            return y + y

        def true_fn(k, pred2):
            z = cond(pred2, true_nested, false_nested, [k])
            return torch.add(torch.tensor([.25, .25]), z)

        def false_fn(k, _):
            return k.cos()

        def f(k, pred, pred2):
            return cond(pred, true_fn, false_fn, [k, pred2])

        x = torch.tensor([0.5, 0.5])
        graph = make_fx(f)(x, torch.tensor(False), torch.tensor(False))

        a = torch.tensor([1.0, 1.0])
        result_true_true = graph.forward(a, torch.tensor(True), torch.tensor(True))
        self.assertEqual(result_true_true, (a * a) + torch.tensor([0.25, 0.25]))

        b = torch.tensor([2.0, 2.0])
        result_true_true = graph.forward(b, torch.tensor(True), torch.tensor(True))
        self.assertEqual(result_true_true, (b * b) + torch.tensor([0.25, 0.25]))

    def test_cond_nested_traced_multi(self):
        def true_a(y):
            return y * y

        def false_a(y):
            return y + y

        def true_b(y, z):
            return y + z

        def false_b(y, z):
            return y * z

        def f(x, pred, pred2):
            a_out = cond(pred, true_a, false_a, [x])
            b_out = cond(pred2, true_b, false_b, [x, x])
            return a_out + b_out

        x = torch.randn(4)
        graph = make_fx(f)(x, torch.tensor(False), torch.tensor(False))

        # Brittle, yet, delicious
        out = """
        def forward(self, x_1, pred_1, pred2_1):
            true_graph_0 = self.true_graph_0
            false_graph_0 = self.false_graph_0
            conditional = torch.ops.cond(pred_1, true_graph_0, false_graph_0, [x_1]);
            pred_1 = true_graph_0 = false_graph_0 = None
            true_graph_1 = self.true_graph_1
            false_graph_1 = self.false_graph_1
            conditional_1 = torch.ops.cond(pred2_1, true_graph_1, false_graph_1, [x_1, x_1]);
            pred2_1 = true_graph_1 = false_graph_1 = x_1 = None
            add = torch.ops.aten.add.Tensor(conditional, conditional_1);  conditional = conditional_1 = None
            return add
        """
        code = graph.code
        # Normalization hack, cause .code makes some weird whitespace
        code = "".join(code.split())
        out = "".join(out.split())
        self.assertEqual(code, out)

        code = graph.true_graph_0.code
        out = """
        def forward(self, y_1):
            mul = torch.ops.aten.mul.Tensor(y_1, y_1);  y_1 = None
            return mul
        """
        # Normalization hack, cause .code makes some weird whitespace
        code = "".join(code.split())
        out = "".join(out.split())
        self.assertEqual(code, out)

    def test_assert_on_mismatch_type_size(self):
        def true_fn(x):
            return x.sin()

        def false_fn(x):
            return (x, x)

        def f(x, y):
            return cond(y, true_fn, false_fn, [x])

        x = torch.randn(4)
        with self.assertRaises(AssertionError):
            make_fx(f)(x, torch.tensor(False))


    def test_assert_on_mismatch_tensor_size(self):
        def true_fn(x):
            return x.sin()

        def false_fn(x):
            return torch.zeros([10, 10])

        def f(x, y):
            return cond(y, true_fn, false_fn, [x])

        x = torch.randn(4)
        with self.assertRaises(AssertionError):
            make_fx(f)(x, torch.tensor(False))

    def test_cond_traced_not_nested_fake_tensor(self):
        def true_fn(x):
            return x.sin()

        def false_fn(x):
            return x.cos()

        def f(x, y):
            return cond(y, true_fn, false_fn, [x])

        x = torch.randn(4)
        graph = make_fx(f, tracing_mode="fake")(x, torch.tensor(False))
        result_true = graph.forward(x, torch.tensor(True))
        result_false = graph.forward(x, torch.tensor(False))
        self.assertFalse(torch.allclose(result_true, result_false))
        self.assertEqual(result_true, torch.sin(x))
        self.assertEqual(result_false, torch.cos(x))

    def test_cond_nested_traced_fake_tensor(self):
        def true_nested(y):
            return y * y

        def false_nested(y):
            return y + y

        def true_fn(x, pred2):
            z = cond(pred2, true_nested, false_nested, [x])
            return x + z

        def false_fn(x, _):
            return x.cos()

        def f(x, pred, pred2):
            return cond(pred, true_fn, false_fn, [x, pred2])

        x = torch.randn(4)
        graph = make_fx(f, tracing_mode="fake")(x, torch.tensor(False), torch.tensor(False))

        result_true_true = graph.forward(x, torch.tensor(True), torch.tensor(True))  # True + True -> x * x
        result_true_false = graph.forward(x, torch.tensor(True), torch.tensor(False))  # True + True -> x + x
        result_false_true = graph.forward(x, torch.tensor(False), torch.tensor(True))  # False + either -> cos
        result_false_false = graph.forward(x, torch.tensor(False), torch.tensor(False))  # False + either -> cos

        self.assertNotEqual(result_true_true, result_true_false)
        self.assertFalse(torch.allclose(result_false_true, result_true_true))

        self.assertEqual(result_false_true, result_false_false)

        self.assertEqual(result_true_true, (x * x) + x)
        self.assertEqual(result_true_false, x + x + x)

        self.assertEqual(result_false_true, torch.cos(x))

    def test_cond_nested_traced_other_inputs_fake_tensor(self):
        def true_nested(y):
            return y * y

        def false_nested(y):
            return y + y

        def true_fn(k, pred2):
            z = cond(pred2, true_nested, false_nested, [k])
            return torch.add(torch.tensor([.25, .25]), z)

        def false_fn(k, _):
            return k.cos()

        def f(k, pred, pred2):
            return cond(pred, true_fn, false_fn, [k, pred2])

        x = torch.tensor([0.5, 0.5])
        graph = make_fx(f, tracing_mode="fake")(x, torch.tensor(False), torch.tensor(False))

        a = torch.tensor([1.0, 1.0])
        result_true_true = graph.forward(a, torch.tensor(True), torch.tensor(True))
        self.assertEqual(result_true_true, (a * a) + torch.tensor([0.25, 0.25]))

        b = torch.tensor([2.0, 2.0])
        result_true_true = graph.forward(b, torch.tensor(True), torch.tensor(True))
        self.assertEqual(result_true_true, (b * b) + torch.tensor([0.25, 0.25]))

    def test_cond_nested_traced_multi_fake_tensor(self):
        def true_a(y):
            return y * y

        def false_a(y):
            return y + y

        def true_b(y, z):
            return y + z

        def false_b(y, z):
            return y * z

        def f(x, pred, pred2):
            a_out = cond(pred, true_a, false_a, [x])
            b_out = cond(pred2, true_b, false_b, [x, x])
            return a_out + b_out

        x = torch.randn(4)
        graph = make_fx(f, tracing_mode="fake")(x, torch.tensor(False), torch.tensor(False))

        # Brittle, yet, delicious
        out = """
        def forward(self, x_1, pred_1, pred2_1):
            true_graph_0 = self.true_graph_0
            false_graph_0 = self.false_graph_0
            conditional = torch.ops.cond(pred_1, true_graph_0, false_graph_0, [x_1]);
            pred_1 = true_graph_0 = false_graph_0 = None
            true_graph_1 = self.true_graph_1
            false_graph_1 = self.false_graph_1
            conditional_1 = torch.ops.cond(pred2_1, true_graph_1, false_graph_1, [x_1, x_1]);
            pred2_1 = true_graph_1 = false_graph_1 = x_1 = None
            add = torch.ops.aten.add.Tensor(conditional, conditional_1);  conditional = conditional_1 = None
            return add
        """
        code = graph.code
        # Normalization hack, cause .code makes some weird whitespace
        code = "".join(code.split())
        out = "".join(out.split())
        self.assertEqual(code, out)

        code = graph.true_graph_0.code
        out = """
        def forward(self, y_1):
            mul = torch.ops.aten.mul.Tensor(y_1, y_1);  y_1 = None
            return mul
        """
        # Normalization hack, cause .code makes some weird whitespace
        code = "".join(code.split())
        out = "".join(out.split())
        self.assertEqual(code, out)

    def test_assert_on_mismatch_type_size_fake_tensor(self):
        def true_fn(x):
            return x.sin()

        def false_fn(x):
            return (x, x)

        def f(x, y):
            return cond(y, true_fn, false_fn, [x])

        x = torch.randn(4)
        with self.assertRaises(AssertionError):
            make_fx(f, tracing_mode="fake")(x, torch.tensor(False))


    def test_assert_on_mismatch_tensor_size_fake_tensor(self):
        def true_fn(x):
            return x.sin()

        def false_fn(x):
            return torch.zeros([10, 10])

        def f(x, y):
            return cond(y, true_fn, false_fn, [x])

        x = torch.randn(4)
        with self.assertRaises(AssertionError):
            make_fx(f, tracing_mode="fake")(x, torch.tensor(False))

    def check_map_graph(self, gm, key):
        i = 0
        for node in gm.graph.nodes:
            if node.op == "call_function" and node.target == torch.ops.map:
                i += 1
                self.assertEqual(
                    node.meta[key].shape[0], node.args[1].meta[key].shape[0]
                )
        self.assertEqual(i, 1)

    def test_map_real(self):
        def f(x, y):
            return x + y

        def g(xs, y):
            return control_flow.map(f, xs, y)

        gm = make_fx(g, tracing_mode="real")(torch.ones(3, 2, 2), torch.ones(2))
        x = torch.randn(3, 2, 2)
        y = torch.randn(2)
        res = gm(x, y)
        self.assertEqual(res, g(x, y))
        self.check_map_graph(gm, "tensor_meta")

    def test_map_symbolic(self):
        def f(x, y):
            return x + y

        def g(xs, y):
            return control_flow.map(f, xs, y)

        gm = make_fx(g, tracing_mode="symbolic")(torch.ones(3, 2, 4), torch.ones(4))
        x = torch.randn(3, 2, 2)
        y = torch.randn(2)
        res = gm(x, y)
        self.assertEqual(res, g(x, y))
        self.check_map_graph(gm, "val")

    def test_map_functionalized(self):
        def map_fn(x, y):
            z = x + y
            z.add_(4)
            return z

        def f(xs, y):
            return control_flow.map(map_fn, xs, y)

        example_inputs = (torch.ones(3, 2, 4), torch.ones(4))
        functional_f = torch.func.functionalize(f)
        self.assertEqual(functional_f(*example_inputs), f(*example_inputs))

        gm = make_fx(torch.func.functionalize(f))(*example_inputs)
        self.assertEqual(gm(*example_inputs), f(*example_inputs))

        gm = make_fx(torch.func.functionalize(f), tracing_mode="symbolic")(*example_inputs)
        self.assertEqual(gm(*example_inputs), f(*example_inputs))

        for node in gm.body_graph_0.graph.nodes:
            if node.op == "call_function":
                self.assertTrue(not node.target._schema.is_mutable)

    def test_map_functionalized_arg_mutation(self):
        def map_fn(x, y):
            y.add_(4)
            return x + y

        def f(xs, y):
            return control_flow.map(map_fn, xs, y)

        example_inputs = (torch.ones(3, 2, 4), torch.ones(4))
        functional_f = torch.func.functionalize(f)
        with self.assertRaisesRegex(UnsupportedAliasMutationException, "torch.map is mutating the input!"):
            functional_f(*example_inputs)

    def test_map_functionalized_elem_mutation(self):
        def map_fn(x, y):
            x.add_(4)
            return x + y

        def f(xs, y):
            return control_flow.map(map_fn, xs, y)

        example_inputs = (torch.ones(3, 2, 4), torch.ones(4))
        functional_f = torch.func.functionalize(f)
        with self.assertRaisesRegex(UnsupportedAliasMutationException, "torch.map is mutating the input!"):
            functional_f(*example_inputs)

    def test_map_functionalized_elem_alias(self):
        def map_fn(x):
            x.view(x.shape)
            return x

        def f(xs):
            return control_flow.map(map_fn, xs)

        example_inputs = (torch.ones(3, 2, 4),)
        functional_f = torch.func.functionalize(f)
        with self.assertRaisesRegex(UnsupportedAliasMutationException, "torch.map is aliasing the input!"):
            functional_f(*example_inputs)

    def test_nested_map_cond_real(self):
        def true_fn(x, y):
            return x * y

        def false_fn(x, y):
            return x + y

        def f(x, pred, y):
            return cond(pred, true_fn, false_fn, [x, y])

        def g(pred, xs, y):
            return control_flow.map(f, xs, pred, y)

        gm = make_fx(g, tracing_mode="real")(
            torch.tensor(True), torch.ones(3, 2, 4), torch.ones(4)
        )
        pred = torch.tensor(False)
        x = torch.randn(3, 2, 2)
        y = torch.randn(2)
        res = gm(pred, x, y)
        self.assertEqual(res, g(pred, x, y))
        self.check_map_graph(gm, "tensor_meta")

    def test_nested_map_cond_symbolic(self):
        def true_fn(x, y):
            return x * y

        def false_fn(x, y):
            return x + y

        def f(x, pred, y):
            return cond(pred, true_fn, false_fn, [x, y])

        def g(pred, xs, y):
            return control_flow.map(f, xs, pred, y)

        gm = make_fx(g, tracing_mode="symbolic")(
            torch.tensor(True), torch.ones(3, 2, 4), torch.ones(4)
        )
        pred = torch.tensor(False)
        x = torch.randn(3, 2, 2)
        y = torch.randn(2)
        res = gm(pred, x, y)
        self.assertEqual(res, g(pred, x, y))
        self.check_map_graph(gm, "val")

    def test_nested_cond_map_cond_symbolic(self):

        def true_fn(x, y):
            return x * y

        def false_fn(x, y):
            return x + y

        def f(x, pred, y):
            return cond(pred, true_fn, false_fn, [x, y])

        def g(pred, xs, y):
            return control_flow.map(f, xs, pred, y)

        def main_true_fn(pred, xs, y):
            return g(pred, xs, y) * 2

        def main_false_fn(pred, xs, y):
            return g(pred, xs, y) + 1

        def main(p, pred, xs, y):
            return cond(p, main_true_fn, main_false_fn, [pred, xs, y])

        gm = make_fx(main, tracing_mode="symbolic")(
            torch.tensor(True), torch.tensor(True), torch.ones(3, 2, 4), torch.ones(4)
        )
        p = torch.tensor(False)
        pred = torch.tensor(False)
        xs = torch.randn(3, 2, 2)
        y = torch.randn(2)
        res = gm(p, pred, xs, y)
        self.assertEqual(res, main(p, pred, xs, y))

    def test_cond_with_sym_pred(self):
        def true_fn(x):
            return x + x

        def false_fn(x):
            return x * x

        def foo(x):
            return cond(x.shape[0] == 4, true_fn, false_fn, [x])

        gm = make_fx(foo, tracing_mode="symbolic")(torch.ones(3, 2, 1))
        x = torch.ones(4, 3, 2)
        self.assertEqual(foo(x), gm(x))

    def test_trace_while_loop(self):
        def cond_fun(iter, val):
            return iter > 0

        def body_fun(iter, val):
            return (iter - 1, val.sin())

        def f(input):
            return control_flow.while_loop(cond_fun, body_fun, input)

        iter = torch.tensor(6)
        val = torch.randn(2, 3)
        input = (iter, val)

        res_eager = f(input)

        gm_symbolic = make_fx(f, tracing_mode="symbolic")(input)
        res_symbolic = gm_symbolic(input)

        gm_real = make_fx(f, tracing_mode="real")(input)
        res_real = gm_real(input)

        while iter > 0:
            val = val.sin()
            iter -= 1
        expected = (0, val)
        self.assertEqual(res_eager, expected)
        self.assertEqual(res_symbolic, expected)
        self.assertEqual(res_real, expected)


    def test_trace_while_loop_nested(self):
        def inner_fun(iter, val):
            return (iter - 1, val + 1)

        def inner_cond_fun(iter, val):
            return iter > 0

        def outter_cond_fun(iter, inner_iter, val):
            return iter > 0

        def outter_fun(iter, inner_iter, val):
            _, val = control_flow.while_loop(inner_cond_fun, inner_fun, (inner_iter, val))
            return (iter - 1, inner_iter.clone(), val)

        iter = torch.tensor(2)
        inner_iter = torch.tensor(5)
        total_iter = iter * inner_iter
        input = (iter, inner_iter, torch.zeros(2, 3))

        def f(input):
            return control_flow.while_loop(outter_cond_fun, outter_fun, input)

        res_eager = f(input)

        gm_symbolic = make_fx(f, tracing_mode="symbolic")(input)
        res_symbolic = gm_symbolic(input)

        gm_real = make_fx(f, tracing_mode="real")(input)
        res_real = gm_real(input)

        val = input[2]
        while total_iter > 0:
            val = val + 1
            total_iter -= 1

        expected = (0, inner_iter, val)
        self.assertEqual(res_eager, expected)
        self.assertEqual(res_symbolic, expected)
        self.assertEqual(res_real, expected)

    def test_trace_functionalize_while_loop(self):
        def cond_fun(iter, val):
            iter_ = iter.view(1) + 1
            iter_.add_(-1)
            return iter_ > 0

        def body_fun(iter, val):
            val_ = val.view(3, 2) + 2
            val_.add_(-1)
            return (iter - 1, val_.view(2, 3))

        def f(input):
            return control_flow.while_loop(cond_fun, body_fun, input)

        iter = torch.tensor(5)
        val = torch.randn(2, 3)
        input = (iter, val)

        res_eager = f(input)

        ff = torch.func.functionalize(f, remove="mutations_and_views")
        res_ff = ff(input)

        gm_symbolic = make_fx(ff, tracing_mode="symbolic")(input)
        res_ff_symbolic = gm_symbolic(input)

        gm_real = make_fx(ff, tracing_mode="real")(input)
        res_ff_real = gm_real(input)

        self.assertEqual(op_count(torch.ops.aten.view.default, gm_symbolic), 0)
        self.assertEqual(op_count(torch.ops.aten.add_.Tensor, gm_symbolic), 0)
        self.assertEqual(op_count(torch.ops.aten.view.default, gm_real), 0)
        self.assertEqual(op_count(torch.ops.aten.add_.Tensor, gm_real), 0)


        while iter > 0:
            val = val + 1
            iter -= 1
        expected = (iter, val)

        self.assertEqual(res_eager, expected)
        self.assertEqual(res_ff, expected)
        self.assertEqual(res_ff_symbolic, expected)
        self.assertEqual(res_ff_real, expected)

    def test_trace_functionalize_nested_while_loop(self):
        def inner_fun(iter, val):
            val_ = val.view(3, 2) + 2
            val_.add_(-1)
            return (iter - 1, val_.view(2, 3))

        def inner_cond_fun(iter, val):
            iter_ = iter.view(1) + 1
            iter_.add_(-1)
            return iter_ > 0

        def outter_cond_fun(iter, inner_iter, val):
            iter_ = iter.view(1) + 1
            iter_.add_(-1)
            return iter_ > 0

        def outter_fun(iter, inner_iter, val):
            _, val = control_flow.while_loop(inner_cond_fun, inner_fun, (inner_iter, val))
            # clone inner_iter to avoid aliasing input
            return (iter - 1, inner_iter.clone(), val)

        iter = torch.tensor(2)
        inner_iter = torch.tensor(5)
        total_iter = iter * inner_iter
        input = (iter, inner_iter, torch.zeros(2, 3))

        def f(input):
            return control_flow.while_loop(outter_cond_fun, outter_fun, input)

        ff = torch.func.functionalize(f, remove="mutations_and_views")
        gm_symbolic = make_fx(ff, tracing_mode="symbolic")(input)
        gm_real = make_fx(ff, tracing_mode="real")(input)

        self.assertEqual(op_count(torch.ops.aten.view.default, gm_symbolic), 0)
        self.assertEqual(op_count(torch.ops.aten.add_.Tensor, gm_symbolic), 0)

        self.assertEqual(op_count(torch.ops.aten.view.default, gm_real), 0)
        self.assertEqual(op_count(torch.ops.aten.add_.Tensor, gm_real), 0)


        res_eager = f(input)
        res_ff = ff(input)
        res_ff_symbolic = gm_symbolic(input)
        res_ff_real = gm_real(input)


        val = input[2]
        while total_iter > 0:
            val = val + 1
            total_iter -= 1

        expected = (0, inner_iter, val)

        self.assertEqual(res_eager, expected)
        self.assertEqual(res_ff, expected)
        self.assertEqual(res_ff_symbolic, expected)
        self.assertEqual(res_ff_real, expected)

if __name__ == '__main__':
    run_tests()
