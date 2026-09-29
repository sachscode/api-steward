# API Steward

Private API modernization with verified behavior preservation, powered by distributed MoE inference.

API Steward is a planned, privately deployed agent that identifies the impact of an API or SDK change, proposes a bounded migration, generates a patch, and presents independent verification evidence for human review.

**Status:** Planning. The first use case is agreed; baseline qualification, model selection, implementation, and measured results are pending.

## First use case

Preserve subscription invoice-preview behavior while upgrading a Python integration to Stripe's Basil API and SDK v12. The migration must preserve the supported customer-facing behavior, use the target API, and avoid billing side effects. Frozen acceptance checks and held-out cases will verify the patch independently of the agent.

The proposed baseline and target versions are fixed historical versions for a reproducible experiment. The baseline must pass qualification before the migration experiment begins. See the [project plan](PROJECT_PLAN.md) for the source revision, versions, and acceptance contract.

## Planned inference system

- Private MoE inference served through vLLM.
- Bounded agent execution with admission control, tenant isolation, cancellation, and request budgets.
- Measured KV/prefix-cache reuse and real Mooncake integration.
- A multi-host demonstration with verified NCCL communication or an RDMA data path.
- Prometheus, Grafana, and tracing to connect output quality, serving performance, GPU/fabric behavior, and cost.
- Controlled comparisons against a simpler serving baseline, including prefill/decode disaggregation.

Model choice, precision, hardware, topology, and performance targets remain open. The planning documents describe requirements, not completed capabilities.

## Project documents

| Document | Purpose |
| --- | --- |
| [Project plan](PROJECT_PLAN.md) | Scope, migration contract, acceptance criteria, and build sequence. |
| [Observability plan](OBSERVABILITY_PLAN.md) | Metrics, concurrency, multi-tenancy, capacity planning, KEDA, quality evaluation, dashboards, and alerts. |
| [Benchmark plan](BENCHMARK_PLAN.md) | Reproducible workloads, quality controls, distributed transport evidence, and comparative experiments. |
| [Demo runbook](DEMO_RUNBOOK.md) | Demo sequence, technical questions, diagnostic drills, and paper-math practice. |
| [Final report plan](FINAL_REPORT_PLAN.md) | Observation records, figures, conclusions, limitations, and reproducibility evidence. |

These files were imported from the existing project planning folder. Keep subsequent versioned planning changes in this repository; the editable Linear copies are not automatically synchronized.

## Tracking

- [Linear project: Private API Maintenance Agent](https://linear.app/chortlelabs/project/private-api-maintenance-agent-917b4f135738/overview)
- [CHO-224: Master epic](https://linear.app/chortlelabs/issue/CHO-224/epic-private-api-maintenance-agent-build-benchmark-demo-and-report)

## Next checkpoint

Qualify and freeze the Stripe invoice-preview baseline and acceptance contract. Then work through checkpointed paper calculations for MoE weight memory, KV-cache capacity, concurrency, prefill/decode demand, transfer bandwidth, and cost before selecting the serving configuration.

There is no runnable agent or deployed inference service in this repository yet.
