# Private API Maintenance Agent

Decision recorded: 2026-09-28

Status: First use case and project direction agreed. Baseline execution, model selection, cloud topology, and implementation are pending.

## Product decision

Build a privately hosted agent that identifies the effect of a documented API change on an application, explains the affected behavior, prepares a patch, and presents independent verification evidence for human review.

The first supported migration is **preserving subscription invoice previews across the Stripe Basil upgrade**. This is the initial use case for ongoing API and SDK maintenance. Broader application modernization remains a possible later expansion.

The project serves two purposes: a useful internally deployable product and an evidence-backed demonstration of production inference engineering. The first version must be buildable and evaluable by one developer using public source code and controlled test data, without access to customer engineering teams.

## First migration

Customer task: A customer selects a different subscription plan and receives the correct estimated invoice before confirming a change.

| Component | Baseline to qualify | Migration target |
| --- | --- | --- |
| Python SDK | `stripe==11.6.0` | `stripe==12.0.0` |
| Explicit Stripe API version | `2025-02-24.acacia` | `2025-03-31.basil` |
| Preview call | `stripe.Invoice.upcoming(...)` | `stripe.Invoice.create_preview(...)` |
| Proposed items parameter | `subscription_items` | `subscription_details.items` |

These are deliberately historical, fixed versions for a reproducible experiment, not a recommendation to deploy an old SDK to production.

Use Stripe's public subscription sample as the source integration:

- Repository: <https://github.com/stripe-samples/subscription-use-cases>
- Source revision: `9056c1e23d1b2cf90257884a62aa43f1a038d789`
- Source route: `/invoice-preview`
- Source file: `fixed-price-subscriptions/server/python/server.py`
- The source revision pins SDK `11.0.0` and explicitly requests API version `2022-08-01`. Establishing the proposed baseline therefore requires a separately recorded preparation step before the migration experiment.

**Baseline qualification is a gate.** Documentation and source inspection have confirmed the migration candidate; the application has not yet been executed. Verify the preview route with the proposed baseline and freeze its behavior before giving the target upgrade to the agent. Record any preparation changes. If that baseline is not viable, document the evidence and revisit the version choice rather than silently changing the experiment.

## Scope boundaries

Start with fixed-price monthly subscriptions, one currency, and controlled billing dates. Evaluate the preview path and the application fields its customer interface consumes. Preserve the application language and existing deployment arrangement while evaluating the API migration.

The initial deliverable does not cover a complete Stripe Basil migration, live billing changes, automatic production deployment, framework rewrites, Python-to-Go translation, or customer application replatforming. The product's own deployment and inference architecture are separate from what it migrates for customers.

No purchased dataset is needed for the first case. Public integration code is real source material; constructed fixtures and Stripe sandbox responses must be labeled as test data, not customer production data.

## Customer workflow

1. Specify the dependency/API upgrade and the behavior that must remain true.
2. Inspect code, dependency versions, explicit API-version overrides, and relevant documentation.
3. Present an impact map linking the billing screen, preview route, subscription lookup, Stripe operation, and displayed estimate.
4. Propose a bounded migration plan and the checks that will establish success.
5. Generate a patch and run tests in an isolated environment with bounded retries.
6. Present the diff, evidence, unresolved issues, and a reviewable change package.

The interface should distinguish observed facts, inferred relationships, customer-confirmed requirements, and unknowns. Each important finding should link to its source revision, documentation, or test evidence. A graph or model explanation alone is not verification.

An initial run can be triggered by a supplied version-change request. Automated monitoring for future releases belongs after this migration workflow is demonstrated.

## Acceptance criteria

| Requirement | Required evidence |
| --- | --- |
| Correct integration context | Requests preserve the intended customer, subscription, and subscription-item IDs. |
| Correct proposed change | Price and quantity changes reach the correct item without changing unrelated items. |
| Equivalent billing meaning | Under controlled time and input state, expected currency, relevant line items, discounts, proration, and totals are preserved for supported scenarios. |
| Compatible customer interface | The application returns the fields consumed by its billing interface with the agreed meanings and types. Full raw Stripe responses need not be identical across versions. |
| Preview has no billing side effects | No actual invoice creation, charge, or subscription modification occurs when requesting a preview. |
| Explicit error behavior | Invalid inputs and provider failures produce the agreed errors rather than misleading successful estimates. |
| Actual target adoption | SDK, request API version, operation, and parameter structure match the target. Downgrading or retaining the old API version does not satisfy the migration. |
| Independent verification | Frozen acceptance checks and held-out scenarios remain outside the agent's editable workspace. |
| Reviewable result | The patch is accompanied by source references, test results, limitations, and reproducible run details. |

Do not preserve an existing defect merely because it appears in the old implementation. Separate any correction to the intended contract from the compatibility change and record the decision.

## Evaluation plan

Begin with scenarios for upgrade, downgrade, unchanged price, quantity change, an existing discount, proration enabled/disabled, invalid identifiers, provider errors/timeouts, and repeated previews without state changes. Fix the proration timestamp and billing inputs before comparing outcomes.

Use three experiment states:

1. Qualified baseline passes the agreed contract checks.
2. The dependency/API upgrade without the migration fails the relevant checks.
3. The agent's patch on the target versions passes those checks and independent regression cases.

Use two verification layers:

- **Local deterministic tests:** Inspect outgoing requests and replay controlled responses. Use these for frequent repair loops and inference workload replay. These verify the application and do not independently establish Stripe's billing calculations.
- **Provider validation:** Use a limited set of synthetic customers and subscriptions in a Stripe testing environment, with controlled billing time, to validate actual API behavior. This requires the developer's own test account and credentials, not company access or production data.

Do not load-test Stripe's API. Replay local cases to measure the migration agent and inference system.

Compare the agent with a deterministic transformation baseline. Measure accepted-patch rate, failed or incorrect success claims, repair attempts, elapsed time, token/GPU cost, and review effort. Report the result as a bounded case study until evaluated on additional independent integrations; multiple fixtures on one repository are not multiple independent repository migrations.

## Inference engineering requirements

Preserve these goals from the original final-project brief:

- Privately hosted MoE inference through the project's own serving path.
- vLLM serving, explicit admission control, request budgets, queueing, cancellation, and placement decisions.
- Measured KV/prefix-cache optimization across analysis and repair steps.
- A multi-host final system that exercises actual NCCL communication or an RDMA data path, with the chosen transport verified.
- Real Mooncake integration appropriate to the selected topology. Metadata-only hop records do not demonstrate KV transfer.
- Prometheus/Grafana and request tracing that connect migration stages to inference latency, cache pressure, worker utilization, transfer behavior, errors, and cost.
- CUDA graphs and custom GPU kernels are optional extensions after profiling identifies a useful target.

Keep model quality and serving efficiency as separate evaluation dimensions. Use a supported vLLM/Mooncake combination and compare against a simpler serving baseline; disaggregation and additional hosts must have measured effects rather than assumed benefits. Serving two replicas does not by itself demonstrate model-parallel collectives or RDMA.

Protect the private-inference boundary across model calls, source retrieval, generated patches, logs, and traces. External provider-validation calls use synthetic billing data; application source does not need to be sent to a hosted model.

Detailed engineering and demo specifications:

- [Observability plan](OBSERVABILITY_PLAN.md): measurement boundaries, metric inventory, tenant/concurrency accounting, capacity and KEDA, quality evaluation, dashboards, alerts and implementation gates.
- [Benchmark plan](BENCHMARK_PLAN.md): reproducible workloads, equal-resource P/D comparison, transport proof, quality controls and failure experiments.
- [Demo runbook](DEMO_RUNBOOK.md): demonstration sequence, likely technical questions, diagnosis and paper-math rehearsal.
- [Final report plan](FINAL_REPORT_PLAN.md): detailed report structure, experiment observation records, figures, conclusions and reproducibility evidence to prepare when the project concludes.

These documents describe planned work. They do not establish deployed instrumentation, actual GPU/KV transfer, measured performance or product correctness.

## Build and learning sequence

1. Qualify and freeze the Stripe preview baseline and acceptance contract.
2. Work through paper exercises with Sachin calculating each step: total versus active MoE parameters, weight memory, KV bytes per token, context/concurrency capacity, prefill/decode demand, transfer bandwidth, and cost. Use bounded checkpoints and review his results before advancing.
3. Select the model, precision, hardware, and topology using the calculations and a small task-quality evaluation.
4. Build the bounded migration loop and independent evaluation harness.
5. Implement the reviewable impact map and evidence interface.
6. Establish a simple serving baseline, then implement and measure the multi-host system, cache behavior, and observability.
7. Package reproducible experiments, failure analysis, architecture decisions, and a concise product demonstration.
8. Write a detailed technical report sharing observations, supported conclusions, unsuccessful approaches, limitations and reproducibility evidence. Collect the observation records throughout the experiments.

## Decisions still open

- Exact MoE model, precision, and context budget.
- GPU provider, interconnect availability, rental budget, and access to course resources.
- Multi-host topology and whether the demonstrated communication path uses NCCL, RDMA, or both.
- Agent framework and implementation language.
- Available project time and the allocation between product work and inference experiments.
- Concrete latency, throughput, quality, and cost targets after baseline measurement.

This record captures the agreed scope. Implementation and validation results will be recorded separately.

## Sources

- [Stripe Python v12 migration guide](https://github.com/stripe/stripe-python/wiki/Migration-guide-for-v12)
- [Pinned source application](https://github.com/stripe-samples/subscription-use-cases/blob/9056c1e23d1b2cf90257884a62aa43f1a038d789/fixed-price-subscriptions/server/python/server.py)
- [SDK v11.6.0 API version](https://github.com/stripe/stripe-python/blob/v11.6.0/stripe/_api_version.py)
- [SDK v12.0.0 API version](https://github.com/stripe/stripe-python/blob/v12.0.0/stripe/_api_version.py)
- [Create Preview API](https://docs.stripe.com/api/invoices/create_preview?api-version=2025-03-31.basil)
- [Stripe automated testing](https://docs.stripe.com/automated-testing)
- [Stripe billing simulations](https://docs.stripe.com/billing/testing/test-clocks)
- [vLLM Mooncake connector](https://docs.vllm.ai/en/latest/features/mooncake_connector_usage/)
- [YC Requests for Startups](https://www.ycombinator.com/rfs)
- [Course final-project page](https://maven.com/goabiaryan/inferencing/2/syllabus/modules/b3f1f2/zzf1kjbw1b)
