"""Print one recorded A100 agent job, call by call, from the committed gateway trace extract.

    python3 cluster/replay_job.py [run] [step-rate]     e.g. colo 12  (defaults: colo 12)

For the demo: the cluster is gone, so this replays what the gateway recorded for a real job.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

M = Path(__file__).resolve().parent.parent / "metrics"


def main() -> None:
    run = sys.argv[1] if len(sys.argv) > 1 else "colo"
    rate = float(sys.argv[2]) if len(sys.argv) > 2 else 12
    jobs = pd.read_json(M / "load" / f"a100-{run}.jsonl", lines=True)
    step = jobs[(jobs.rate == rate) & (jobs.status == "analysis_complete")]
    t = pd.read_csv(M / "a100-gateway-traces.csv.gz", low_memory=False)
    t = t[(t.ts >= step.start.min()) & (t.ts <= (step.start + step.duration_s).max()) & (t.status == 200)]
    four = t.groupby("job_id").filter(lambda g: len(g) == 4)
    job = four[four.job_id == four.job_id.iloc[len(four) // 2]].sort_values("step")
    cols = ["step", "tenant", "klass", "decode", "hop_mode", "prompt_tokens", "completion_tokens",
            "queue_wait_decode_s", "ttft_s", "engine_ttft_s", "e2e_s"]
    print(f"run {run}, target {rate:g} jobs/s, job {job.job_id.iloc[0]}")
    print(job[cols].to_string(index=False))
    print(f"job wall time ~ {job.ts.max() - (job.ts.min() - job.e2e_s.iloc[0]):.1f} s over 4 non-streaming calls")


if __name__ == "__main__":
    main()
