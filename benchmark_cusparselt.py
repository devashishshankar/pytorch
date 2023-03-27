import os
import sys
from itertools import product, combinations, combinations_with_replacement, permutations
import torch
import torch.utils.benchmark as benchmark
from torch import nn
from torch.ao.pruning import WeightNormSparsifier
from torch.ao.nn.sparse.cusparselt_linear import cuSPARSELtLinear
from torch.profiler import profile, record_function, ProfilerActivity
from pprint import pprint
from time import time
from tqdm import tqdm
import pandas as pd
import argparse
import gc

device = "cuda"
torch.set_printoptions(
    precision=3,
    threshold=None,
    edgeitems=4,
    linewidth=460,
    profile=None,
    sci_mode=False,
)


# helper model definition for pruner
class Model(nn.Module):
    def __init__(self, m, k, dtype=None):
        super().__init__()
        # transposed so reversed
        self.linear = nn.Linear(k, m)

    def forward(self, x):
        return self.linear(x)


# compare different dtypes
def compare_dtype(m, k, n, batch_size, dtype):

    model = Model(m, k).type(dtype).eval().cuda()

    input_tensor = torch.randint(
        10,
        (batch_size, n, k),
        device=model.linear.weight.device,
        dtype=dtype,
    )


    latency = benchmark.Timer(
        stmt="model(input_tensor)",
        globals={"input_tensor": input_tensor, "model": model}
    ).blocked_autorange()

    return {
        "m": m,
        "k": k,
        "n": n,
        "eval_batch_size": batch_size,
        "dtype": str(dtype),
        "latency (ms)": latency.median * 1000,
    }

def compare_memory(m, k, n, batch_size):
    print("+"*100)
    print(f"start: {sizeof_fmt(torch.cuda.memory_allocated())}")
    print(torch.cuda.memory_summary())
    # create dense model
    model = Model(m, k).half().cuda().eval()
    print("+"*100)
    print(f"model: {sizeof_fmt(torch.cuda.memory_allocated())}")
    print(torch.cuda.memory_summary())

    # create input tensor
    input_tensor = torch.randn(
        batch_size,
        n,
        k,
        device=model.linear.weight.device,
        dtype=model.linear.weight.dtype,
    )
    print("+"*100)
    print(f"input: {sizeof_fmt(torch.cuda.memory_allocated())}")
    print(torch.cuda.memory_summary())

    
    # get sparse model
    print(f"sparse start: {sizeof_fmt(torch.cuda.memory_allocated())}")
    pruner = WeightNormSparsifier(
        sparsity_level=1.0, sparse_block_shape=(1, 4), zeros_per_block=2
    )
    pruner.prepare(model, [{"tensor_fqn": "linear.weight"}])
    pruner.step()
    print("+"*100)
    print(f"step: {sizeof_fmt(torch.cuda.memory_allocated())}")
    print(torch.cuda.memory_summary())

    sparse_model = pruner.convert(model, mapping={nn.Linear: cuSPARSELtLinear}, inplace=False)

    sparse_model.load_state_dict(torch.load("sparse_model.pt"))

    print(model)
    print("+"*100)
    print(f"convert: {sizeof_fmt(torch.cuda.memory_allocated())}")
    print(torch.cuda.memory_summary())


    # zero out dense tensor weights for correctness check
    pruner.squash_mask()
    model.load_state_dict(torch.load("dense_model.pt"))
    print("+"*100)
    print(f"squash: {sizeof_fmt(torch.cuda.memory_allocated())}")
    print(torch.cuda.memory_summary())

    del pruner
    torch.cuda.empty_cache()
    print("+"*100)
    print(f"del pruner: {sizeof_fmt(torch.cuda.memory_allocated())}")
    print(torch.cuda.memory_summary())


    assert torch.allclose(
        model(input_tensor), sparse_model(input_tensor), rtol=1e-3, atol=1e-3
    )

    alg_id = sparse_model.linear.cslt.get_alg_id()
    print(model)

    # del model
    # torch.cuda.empty_cache()
    # print("+"*100)
    # print(f"del model: {sizeof_fmt(torch.cuda.memory_allocated())}")
    # print(torch.cuda.memory_summary())

    # sparse_model(input_tensor)

    # del input_tensor
    # torch.cuda.empty_cache()
    # print("+"*100)
    # print(f"del input: {sizeof_fmt(torch.cuda.memory_allocated())}")
    # print(torch.cuda.memory_summary())

    # del sparse_model 
    # torch.cuda.empty_cache()
    # print("+"*100)
    # print(f"del sparse: {sizeof_fmt(torch.cuda.memory_allocated())}")
    # print(torch.cuda.memory_summary())
    torch.save(sparse_model.state_dict(), "sparse_model.pt")
    torch.save(model.state_dict(), "dense_model.pt")
    from pprint import pprint
    pprint(torch.load("sparse_model.pt"))
    # sparse_model_2(input_tensor)

    return {
        "m": m,
        "k": k,
        "n": n,
        "eval_batch_size": batch_size,
        "init_batch_size": batch_size,
        "alg_id": alg_id,
        "sparse_model_size": sizeof_fmt(os.stat("sparse_model.pt").st_size), 
        "dense_model_size": sizeof_fmt(os.stat("dense_model.pt").st_size), 
    }




def sizeof_fmt(num, suffix="B"):
    for unit in ["", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"]:
        if abs(num) < 1024.0:
            return f"{num:3.1f}{unit}{suffix}"
        num /= 1024.0
    return f"{num:.1f}Yi{suffix}"

# function to compare dense vs cusparselt linear for given m, k, n, batch_size
def compare_linear(m, k, n, batch_size, init_batch_size=None):
    # create dense model
    model = Model(m, k).half().cuda().eval()
    # create input tensor
    input_tensor = torch.randn(
        init_batch_size if init_batch_size else batch_size,
        n,
        k,
        device=model.linear.weight.device,
        dtype=model.linear.weight.dtype,
    )
    
    # get sparse model
    pruner = WeightNormSparsifier(
        sparsity_level=1.0, sparse_block_shape=(1, 4), zeros_per_block=2
    )

    pruner.prepare(model, [{"tensor_fqn": "linear.weight"}])
    pruner.step()
    sparse_model = pruner.convert(model, mapping={nn.Linear: cuSPARSELtLinear})

    # zero out dense tensor weights for correctness check
    pruner.squash_mask()

    devnull = open("/dev/null", "w")
    oldstdout_fno = os.dup(sys.stdout.fileno())
    os.dup2(devnull.fileno(), 1)
   
    assert torch.allclose(
        model(input_tensor), sparse_model(input_tensor), rtol=1e-3, atol=1e-3
    )

    # get alg_id
    alg_id = sparse_model.linear.cslt.get_alg_id()

    input_tensor = torch.randn(
        batch_size,
        n,
        k,
        device=model.linear.weight.device,
        dtype=model.linear.weight.dtype,
    )
    # get latency
    sparse_measurement = benchmark.Timer(
        stmt="sparse_model(input_tensor)",
        globals={"input_tensor": input_tensor, "sparse_model": sparse_model},
    ).blocked_autorange()
    dense_measurement = benchmark.Timer(
        stmt="model(input_tensor)",
        globals={"input_tensor": input_tensor, "model": model},
    ).blocked_autorange()

    os.dup2(oldstdout_fno, 1)
    del input_tensor
    torch.cuda.empty_cache()
    del model
    torch.cuda.empty_cache()
    del sparse_model
    torch.cuda.empty_cache()

    return {
        "m": m,
        "k": k,
        "n": n,
        "eval_batch_size": batch_size,
        "init_batch_size": init_batch_size if init_batch_size else batch_size,
        "alg_id": alg_id,
        "sparse_latency (ms)": sparse_measurement.median * 1000,
        "dense_latency (ms)": dense_measurement.median * 1000,
        "speedup (d/s)": dense_measurement.median / sparse_measurement.median,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="cuSPARSELt Benchmarks")
    parser.add_argument(
        "--mode",
        type=str,
        choices=[
            "nvidia-bert",
            "nvidia-fixed-k",
            "nvidia-fixed-mn",
            "distilbert-shapes",
            "alg-id-sweep",
            "int8-fp16-linear",
            "memory"
        ],
    )
    args = parser.parse_args()

    print(f"Started benchmark: {args.mode}")

    if args.mode == "nvidia-bert":
        bert_shapes = [
            (3072, 1024, 16384),
            (4096, 1024, 16384),
            (1024, 1024, 16384),
            (1024, 4096, 16384),
        ]
        results = (compare_linear(m, k, n, 1) for (m, k, n) in tqdm(bert_shapes))

    elif args.mode == "nvidia-fixed-k":
        mn_vals = [
            3072,
            4096,
            5120,
            6144,
            7168,
            8192,
            9216,
            10240,
            11264,
            12288,
            13312,
            14336,
            15360,
            16384,
            17408,
            18432,
            19456,
            20480,
        ]
        results = (compare_linear(mn, 10240, mn, 1) for mn in tqdm(mn_vals))

    elif args.mode == "nvidia-fixed-mn":
        k_vals = [
            2560,
            3840,
            5120,
            6400,
            7680,
            8960,
            10240,
            11520,
            12800,
            14080,
            15360,
            16640,
            17920,
            19200,
            20480,
        ]
        results = (compare_linear(10240, k, 10240, 1) for k in tqdm(k_vals))

    elif args.mode == "distilbert-shapes":
        shapes = [
            # distilbert shapes
            (768, 3072, 3072),
            (3072, 768, 3072),
            # jiecao shapes
            # (1024, 1536, 2048),
            # (1024, 9408, 2048),
            # (1024, 3200, 2048),
            # (1024, 256, 9472),
            # (1024, 10240, 256),
            # (1024, 256, 12608),
            # (1024, 2560, 1024),
            # (1024, 512, 10240),
            # (1024, 10240, 512),
            # (1024, 2048, 1024),
            # (1024, 512, 512),
            # (1024, 1024, 1024),
            # (1024, 2048, 2048),
            # (2048, 1536, 2048),
            # (2048, 9408, 2048),
            # (2048, 3200, 2048),
            # (2048, 256, 9472),
            # (2048, 10240, 256),
            # (2048, 256, 12608),
            # (2048, 2560, 1024),
            # (2048, 512, 10240),
            # (2048, 10240, 512),
            # (2048, 2048, 1024),
            # (2048, 512, 512),
            # (2048, 1024, 1024),
            # (2048, 2048, 2048),
        ]
        batch_sizes = [4, 16, 64, 256]
        results = (
            compare_linear(m, k, n, batch_size)
            for (m, k, n), batch_size in tqdm(
                product(shapes, batch_sizes), total=len(shapes) * len(batch_sizes)
            )
        )

    # run a sweep for the n, batch_size combination
    # then try running batch size different from the initialized batch size to see effect of caching alg plan.
    elif args.mode == "alg-id-sweep":
        dim_range = list(range(96, 3072 + 1, 96))
        batch_sizes = list(range(4, 128 + 1, 4))
        results = [
            compare_linear(768, 3072, n, batch_size)
            for n, batch_size in tqdm(
                product(dim_range, batch_sizes), total=len(dim_range) * len(batch_sizes)
            )
        ]

        results += [
            compare_linear(768, 3072, 96, batch_size, init_batch_size=init_batch_size)
            for batch_size, init_batch_size in tqdm(product(batch_sizes, batch_sizes), total=len(batch_sizes)**2)
        ]

    elif args.mode == "memory":
        results = [compare_memory(4096, 4096, 4096, 1)]
   
    # TODO this is still a wip
    elif args.mode == "int8-fp16-linear":
        dtypes = [torch.float32, torch.float16]
        batch_sizes = [4, 16, 64, 256]
        results = (
            compare_dtype(768, 3072, 768, batch_size, dtype) 
            for batch_size, dtype in tqdm(product(batch_sizes, dtypes), total=len(dtypes)*len(batch_sizes))
        )

    save_file = f"{args.mode}.csv"
    df = pd.DataFrame.from_records(results)
    df.to_csv(save_file)
    print(f"Finished benchmark: {args.mode} saved results to {save_file}")
    print(df.groupby(["m", "k", "n", "eval_batch_size"]).first())
