"""NCCL all-reduce sweep with torch.distributed, for one host or across hosts.

Run inside the engine image (it already has torch + NCCL), one launcher per host:

    torchrun --nnodes 2 --nproc-per-node 8 --node-rank <0|1> \
        --master-addr <host A private IP> --master-port 29500 nccl_allreduce.py

Prints size, time, algbw and busbw like nccl-tests, so the numbers compare directly with the
probe's all_reduce_perf. Set NCCL_DEBUG=INFO to log which transport NCCL picked
(NET/IB ... RoCE, or NET/Socket), and NCCL_IB_DISABLE=1 to force the TCP socket path.
"""

import os
import time

import torch
import torch.distributed as dist


def main():
    dist.init_process_group("nccl")
    rank, world = dist.get_rank(), dist.get_world_size()
    local = int(os.environ.get("LOCAL_RANK", 0))
    torch.cuda.set_device(local)
    nodes = world // int(os.environ.get("LOCAL_WORLD_SIZE", world))

    sizes = [2**p for p in range(3, 31, 2)]  # 8 B .. 512 MiB, every 4x
    if rank == 0:
        print(f"# world={world} ranks, nodes={nodes}, NCCL_IB_DISABLE={os.environ.get('NCCL_IB_DISABLE', '0')}")
        print(f"# {'size (B)':>12} {'time (us)':>11} {'algbw GB/s':>11} {'busbw GB/s':>11}")
    for size in sizes:
        x = torch.ones(max(1, size // 4), dtype=torch.float32, device="cuda")
        iters = 20 if size < 2**24 else 5
        for _ in range(3):
            dist.all_reduce(x)
        torch.cuda.synchronize()
        dist.barrier()
        t0 = time.perf_counter()
        for _ in range(iters):
            dist.all_reduce(x)
        torch.cuda.synchronize()
        dt = (time.perf_counter() - t0) / iters
        algbw = x.numel() * 4 / dt / 1e9
        busbw = algbw * 2 * (world - 1) / world
        if rank == 0:
            print(f"  {x.numel() * 4:>12} {dt * 1e6:>11.1f} {algbw:>11.2f} {busbw:>11.2f}")
    dist.destroy_process_group()


if __name__ == "__main__":
    main()
