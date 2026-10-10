# API Steward

A tool-using API-migration agent served by a private inference cluster: a control plane I wrote
(guardrails, admission, placement, queueing, KV hop, warm-up, overflow) in front of Qwen3-30B-A3B
(MoE, TP=4) on vLLM, with prefill/decode disaggregation over NIXL, KEDA scaling, Prometheus and
Grafana. Final project for *AI Inference Engineering & Systems Design* (Maven).

**Start with [DESIGN.md](DESIGN.md)**: the design decisions, the measured results, and the
evidence behind each claim.

## Layout

| Path | Contents |
| --- | --- |
| `app/` | The agent (talks only to the gateway) and an agent-shaped open-loop load generator |
| `control/` | The gateway: `guard`, `admit`, `place`, `queue`, `hop`, `fleet`, `overflow`, `serve` |
| `cluster/` | Engine image, k3s/HAMi/KEDA/observability bring-up, engine manifests, dashboards, probes, experiment runner ([cluster/README.md](cluster/README.md)) |
| `notebook/` | Part 5 notebook executed against the A10 rehearsal and the A100 host |
| `plots/`, `metrics/` | Figures, load runs, per-experiment analyses, scrapes, engine logs |
| `tests/` | 28 tests against a fake vLLM fleet |

## Headline results (one 8x A100 40 GB host, Qwen3-30B-A3B)

* Paper sizing predicted vLLM's KV capacity within 2 % (865k vs 878,464 tokens).
* Moving KV over NIXL (~4.5 GB/s via the RoCE NIC) never beat recomputing the prompt locally.
* Colocated engines beat the prefill/decode split on the same 8 GPUs: 20 vs 12 jobs/s within a
  2 s p99 TTFT (about 1.7× cheaper per job) and 13× lower TTFT p99 at 20 jobs/s, with the same
  inter-token latency.
* The first bottleneck was the gateway's own settings, not the GPUs or KV (KV never above 15 %).
* KEDA scaled within 21 s, but a 30B replica took ~3.6 min to become ready.

## Run the tests

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest pytest-asyncio
.venv/bin/python -m pytest -q
```
