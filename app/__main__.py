"""API Steward application. It talks only to the gateway (control/), never to an engine.

python -m app run   --gateway-url http://127.0.0.1:8080
python -m app batch --gateway-url http://127.0.0.1:8080 --jobs 4 --tenant sweep
"""

import argparse
import json
from pathlib import Path

from .agent import DEFAULT_TASK, GatewayClient, run_agent


def emit(runs, output):
    for run in runs:
        print(f"{run['workload_class']}: {run['status']} ({run['reason']}) in {run['duration_s']:.1f}s")
        for step in run["steps"]:
            hop = step.get("hop") or {}
            print(
                f"  step {step['step']}: {' -> '.join(step['stages'])}  decode={step.get('decode')}"
                f"  hop={step.get('hop_mode')}/{step.get('hop_reason')}"
                + (f" blocks={hop.get('blocks')}" if hop else "")
                + f"  ttft={step.get('ttft_s')}s  prompt_tokens={step.get('prompt_tokens')}"
            )
        print(run["answer"] or "No final answer.")
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"runs": runs}, indent=2) + "\n")
        print(f"Run records: {path}")
    return 0 if all(r["status"] == "analysis_complete" for r in runs) else 1


def main():
    parser = argparse.ArgumentParser(description="API Steward: Track B tool-using agent")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "batch"):
        p = sub.add_parser(name)
        p.add_argument("--gateway-url", default="http://127.0.0.1:8080")
        p.add_argument("--model", default="steward")
        p.add_argument("--tenant", default="demo-a")
        p.add_argument("--question", default=DEFAULT_TASK)
        p.add_argument("--output")
        if name == "batch":
            p.add_argument("--jobs", type=int, choices=range(1, 11), default=2)
    args = parser.parse_args()
    client = GatewayClient(args.gateway_url, args.model)
    klass = "batch" if args.command == "batch" else "interactive"
    runs = [
        run_agent(client, question=args.question, tenant=args.tenant, workload_class=klass)
        for _ in range(args.jobs if args.command == "batch" else 1)
    ]
    return emit(runs, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
