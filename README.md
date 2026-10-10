# API Steward

A private, tool-using migration-assessment agent served by an inference cluster: a control plane I wrote
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
| `tests/` | 30 tests against a fake vLLM fleet |

## Headline results (one 8x A100 40 GB host, Qwen3-30B-A3B)

* Paper sizing predicted vLLM's KV capacity within 2 % (865k vs 878,464 tokens).
* For fresh prompts, the full split path (remote prefill + NIXL transfer at ~4.5 GB/s over RoCE NIC
  loopback + first token) was slower than recomputing locally at every size tested.
* Colocated engines beat the prefill/decode split on the same 8 GPUs: at a 20 jobs/s target,
  per-call TTFT p99 1.2 s vs 16.7 s and completed-job median 12.1 s vs 29.0 s, with the same
  inter-token latency. The split met a 2 s p99 only at the 12/s step (estimated 1.1-1.6× the cost
  per job; the load generator fell behind under overload, see DESIGN.md E4).
* The first bottleneck was the gateway's own settings, not the GPUs or KV (KV never above 15 %).
* KEDA scaled within 21 s, but a 30B replica took ~3.6 min to become ready (from recorded k8s events).

## Run the tests

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest pytest-asyncio
.venv/bin/python -m pytest -q
```
