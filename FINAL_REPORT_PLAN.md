# Final technical report plan

Decision recorded: 2026-09-28. Write the completed report after the project experiments conclude. This document defines its structure and the evidence to collect; it contains no experimental findings.

Working title: **Private API Maintenance with MoE Inference: Verification, Distributed Serving, and the Cost of Correct Migrations**.

## Purpose and audience

Produce a detailed, shareable engineering report explaining what we built, how we evaluated it, what we observed, and what conclusions the evidence supports. Make it useful to inference/platform engineers considering internal deployment and to technical interviewers assessing design judgment and implementation depth.

Keep the central question concrete: **How reliably and economically can a privately hosted agent produce a behavior-preserving API migration, and which serving and inference policies improve that outcome?**

The report covers the agreed Stripe invoice-preview migration. Its experimental boundaries follow [PROJECT_PLAN.md](PROJECT_PLAN.md), [OBSERVABILITY_PLAN.md](OBSERVABILITY_PLAN.md), and [BENCHMARK_PLAN.md](BENCHMARK_PLAN.md).

## Planned report structure

| Part | Content | Required evidence |
| --- | --- | --- |
| Executive overview | Customer problem, implemented system, principal measured findings and practical limits | Traceable results; write this section last |
| Problem and correctness contract | Source integration, breaking change, preserved behavior, privacy boundary and solo-project constraints | Pinned source/SDK/API versions, baseline qualification, independent acceptance cases |
| Architecture and decisions | Agent workflow, selected MoE, vLLM, Mooncake, deployment topology, admission, tenancy and KEDA | Actual deployed diagram, version/configuration manifest, alternatives and decision rationale |
| Paper estimates and measured capacity | Weight and KV memory, concurrency, prefill/decode demand, network transfer and cost | Derivations with assumptions, measured values, explanation of discrepancies |
| Evaluation methodology | Workload provenance, local versus provider validation, baselines, SLOs, budgets and measurement boundaries | Frozen manifests, raw request/job data, protected tests, sampling and repeatability details |
| Serving and systems findings | Cache reuse, P/D tradeoffs, actual transport, tenant contention, scaling readiness and failure recovery | Matched experiments, metrics, traces, profiles and negative controls |
| Agent quality and inference-compute findings | Single attempt versus bounded repair versus alternative candidates; selection quality and cost | Candidate coverage, selected-patch acceptance, false acceptance, budgets and actual costs |
| Operational and commercial implications | Deployment requirements, isolation, observability overhead, cost per accepted patch and reviewer effort | Tested behavior and explicit estimates; separate technical feasibility from unvalidated customer demand |
| Failure analysis and limitations | Failed optimizations, unexpected bottlenecks, inconclusive tests, confounders and generalization limits | Preserved failed runs, sample counts, scope of provider checks and hardware/software constraints |
| Conclusions and next steps | Which hypotheses held, which did not, and what should change in a subsequent implementation | Every conclusion linked to an experiment; future work prioritized by observed gaps |
| Reproducibility appendix | Versions, configuration, commands, metric dictionary, acceptance protocol and evidence index | Sanitized artifacts, hashes and instructions sufficient to reproduce supported claims |

## Questions the report must answer

1. Did the final selected patch preserve the agreed invoice-preview behavior on the supported cases?
2. What did extra inference compute buy, and could our selector identify the correct candidate when one existed?
3. Did cache reuse save actual computation and end-to-end time? Under which prompt and placement conditions?
4. Did disaggregated prefill improve useful service at equal resources? Where did transfer or coordination erase the benefit?
5. Which resource limited sustainable concurrency, and how closely did the paper calculation predict it?
6. Did tenant policies and KEDA improve service during contention and bursts, including model warmup and unavailable capacity?
7. What happened during cancellation, worker/transfer failure and telemetry loss?
8. What did one independently accepted migration cost, including failures, idle time and review effort?

## Evidence to capture while working

After each named experiment, add a short observation record to its existing evidence bundle:

- **Question and hypothesis:** written before seeing the result.
- **Configuration and comparison:** what changed, what stayed fixed and any unavoidable differences.
- **Observation:** measured values, units, denominator, sample count, uncertainty and raw-data links.
- **Interpretation:** proposed explanation, supporting trace/profile, competing explanations and unresolved confounders.
- **Decision:** retain, revise, reject or investigate the change; record why.
- **Limit:** exactly what the result does not establish.

Preserve the initial failed attempt and subsequent fixes. Export the metrics/traces needed for the report before their retention windows expire. Record test-suite changes and keep the original evaluation cohort identifiable.

Keep development feedback separate from held-out evaluation. The agent may use development checks to repair/select a candidate. Freeze that selection before running held-out checks; do not feed their answers back into that experiment. Offline candidate-coverage analysis may score all retained candidates afterward without changing the selected result.

## Planned figures and tables

Prefer a small number of informative figures over dashboard screenshots without explanation:

1. Implemented architecture and one complete job trace.
2. Calculated versus observed memory and concurrency capacity.
3. Offered load versus goodput and latency, with rejection/error counts.
4. Matched colocated/P/D comparison, showing cache state and transfer cost.
5. Tenant slowdown under contention and a burst-to-warm-capacity timeline.
6. Single-attempt, repair and alternative-candidate quality versus cost/time.
7. Candidate coverage versus selected-patch success and false acceptance.
8. Failure/recovery timeline and cost breakdown per accepted patch.

Each caption identifies the experiment, hardware, workload, observation count, relevant configuration and supported conclusion. Use standalone exported plots and machine-readable source data. Screenshots can illustrate the operator experience but do not replace quantitative results.

## Reporting rules

- Distinguish measured findings, estimates, interpretations and open hypotheses.
- Include unchanged or worse results; an optimization has no obligation to win.
- Report errors, timeouts, rejections, abandoned work and pending evaluations alongside successful completions.
- Do not describe repeated fixtures from one repository as many independent real-world migrations.
- Keep local replay correctness and actual provider-sandbox evidence distinct.
- Support a transport claim with actual data-path evidence, and a scaling claim with usable serving capacity.
- Separate observed quality from model confidence or persuasive explanations.
- Report distributions and uncertainty appropriate to the sample size. Avoid strong p99 claims from a handful of jobs.
- State which configurations and hypotheses were not tested. Freeze model weights for the bounded inference-policy study.
- Sanitize logs, traces and artifacts for the shareable report. Preserve access-controlled originals when needed for reproduction.

## Completion workflow

1. Close the experiment set and reconcile its evidence index, including failed and unrun cases.
2. Reproduce the figures/tables from the saved raw records and audit metric definitions/denominators.
3. Draft the report around supported findings, then review alternative explanations and limitations.
4. Check each headline claim against its linked evidence; write the executive overview and conclusions last.
5. Prepare the detailed shareable report with its reproducibility appendix. A concise portfolio or presentation summary can reuse this evidence if desired.

The report is a final project deliverable. Collecting observation notes is part of each existing experiment, not an additional product feature or a separate research program.
