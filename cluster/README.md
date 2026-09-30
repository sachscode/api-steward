# cluster/

How the engines and control plane come up on Lambda. Everything runs on the Lambda hosts; the Mac only syncs files and opens SSH tunnels.

## A10 session (prep + rehearsal, ~$1.29/hr)

Launch a 1x A10 in us-west-1 with `steward-fs` attached. Set `LAMBDA_PREP` in `.env`, then sync the whole repo from its root on the Mac (the gateway pod mounts it from `~/api-steward`):

```bash
source .env && rsync -az --exclude .venv --exclude .git --exclude .env --exclude metrics --exclude '__pycache__' --exclude .ruff_cache -e "ssh -i $LAMBDA_SSH_KEY" ./ "$LAMBDA_PREP:~/api-steward/"
```

Scripts below live in `~/api-steward/cluster/` on the host.

On the A10:

| Step | Command | What it proves |
| --- | --- | --- |
| 1 | `bash ~/api-steward/cluster/a10_prep.sh` | Weights and engine image are on steward-fs; filesystem read speed |
| 2 | `bash ~/api-steward/cluster/k3s_up.sh rehearsal` | k3s, HAMi, KEDA, Prometheus, Grafana and the image import work |
| 3 | `bash ~/api-steward/cluster/rehearse.sh` | Engines start from our image; NixlConnector prefill → decode hop works; nixl metrics move |
| 4 | `bash ~/api-steward/cluster/gateway_up.sh rehearsal` | The gateway runs as its own pod (orch-serve) in front of the engines |
| 5 | `cd ~/api-steward && python3 -m app run` | The agent's requests go through the in-cluster serve path |

Terminate the A10 afterwards. steward-fs keeps the weights, image tar and logs.

## Files

| File | Purpose |
| --- | --- |
| `Dockerfile.vllm-nixl` | vLLM v0.30.0 + nixl 1.4.1 + RDMA tools |
| `a10_prep.sh` | Download weights, build and save the image, measure steward-fs reads |
| `k3s_up.sh` | k3s server and add-ons (`rehearsal` on the A10, `prefill` on host A) |
| `stage_weights.sh` | Parallel copy of weights from steward-fs to local NVMe (~630 MB/s vs 74 MB/s single stream) |
| `render_engines.py` | Engine Deployments, Services, KEDA ScaledObjects and the gateway (`rehearsal` or `a100` profile) |
| `Dockerfile.gateway`, `gateway_up.sh` | Gateway runtime image and deploy script; code is mounted from the synced repo |
| `rehearse.sh` | Deploy rehearsal engines and run `pd_smoke.py` |
| `pd_smoke.py` | One request through prefill → KV hop → decode, with /metrics evidence |
| `probe_host.sh` | Hardware probe: GPUs, NVLink, NIC, RDMA, NCCL all-reduce |
| `k8s/observability/` | Prometheus, Grafana and DCGM exporter settings (from class10) |

## A100 session (2× 8x A100 40GB, ~$31.84/hr for both)

Launch both hosts in us-west-1 with `steward-fs` attached, set `LAMBDA_A` and `LAMBDA_B`, rsync `cluster/` to both.

| Step | Host | Command | What it proves |
| --- | --- | --- | --- |
| 1 | B, then A | `bash ~/api-steward/cluster/two_host_net.sh server <A ip>` / `client <B ip>` | RoCE RDMA between hosts (ib_write_bw); 16-GPU NCCL all-reduce over RoCE vs TCP |
| 2 | A | `bash ~/api-steward/cluster/k3s_up.sh prefill` | k3s server and add-ons on host A |
| 3 | B | `bash ~/api-steward/cluster/k3s_join.sh <A ip> <token>` | Host B joins as the decode node |
| 4 | A | `python3 ~/api-steward/cluster/render_engines.py a100 \| kubectl apply -f -` | TP=4 MoE engines, prefill on A, decode on B |
| 5 | A | `python3 ~/api-steward/cluster/pd_smoke.py --decode http://<B ip>:8001` | Real KV hop across hosts; nixl bytes in /metrics |

Use private IPs (the `10.x` address on `eno1`), not the public ones.
