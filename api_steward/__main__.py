import argparse
import json
import os
from pathlib import Path
from threading import Event

from .agent import DEFAULT_TASK, GatewayClient, run_agent
from .serving import Gateway, running_server
from .workers import CompatibleWorker, ScriptedWorker


def emit(runs, output):
    for run in runs:
        print(f"{run['workload_class']}: {run['status']} ({run['reason']})")
        for step in run["steps"]:
            print(
                f"  step {step['step']}: {step['message_count']} messages, "
                f"{step['context_chars']} context characters; " + " -> ".join(step["stages"])
            )
        print(run["answer"] or "No final answer.")
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"runs": runs}, indent=2) + "\n")
        print(f"Run records: {path}")
    return 0 if all(r["status"] == "analysis_complete" for r in runs) else 1


def main():
    parser = argparse.ArgumentParser(description="API Steward Part 0: Track B application")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="Local HTTP path with explicitly scripted completions")
    demo.add_argument("--output")
    serve = sub.add_parser("serve", help="Loopback-only reference gateway")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--backend", choices=["scripted", "compatible"], default="scripted")
    serve.add_argument("--model", default="fixture-model")
    serve.add_argument("--worker-url", help="Private worker base URL without /v1; server-side only")
    for name in ("run", "batch"):
        p = sub.add_parser(name)
        p.add_argument("--gateway-url", default="http://127.0.0.1:8080")
        p.add_argument("--model", default="fixture-model")
        p.add_argument("--tenant", choices=["demo-a", "demo-b"], default="demo-a")
        p.add_argument("--question", default=DEFAULT_TASK)
        p.add_argument("--output")
        if name == "batch":
            p.add_argument("--jobs", type=int, choices=range(1, 11), default=2)
    args = parser.parse_args()
    if args.command == "serve":
        if args.backend == "compatible" and (not args.worker_url or args.model == "fixture-model"):
            parser.error("Compatible mode requires --worker-url and a real --model; there is no fallback")
        if args.backend == "scripted" and args.worker_url:
            parser.error("Use --backend compatible to configure a worker URL")
        worker = (
            ScriptedWorker()
            if args.backend == "scripted"
            else CompatibleWorker(args.worker_url, os.environ.get("STEWARD_WORKER_API_KEY", ""))
        )
        with running_server(Gateway(worker, model=args.model), args.port) as url:
            print(
                f"Local Part 0 gateway: {url}; backend={worker.kind}. No streaming/TTFT measurement.",
                flush=True,
            )
            try:
                Event().wait()
            except KeyboardInterrupt:
                pass
        return 0
    if args.command == "demo":
        print("SCRIPTED FIXTURE: no model inference, GPU, Stripe calls, or billing verification.")
        with running_server(Gateway(ScriptedWorker())) as url:
            client = GatewayClient(url)
            runs = [
                run_agent(client, workload_class="interactive", tenant="demo-a"),
                run_agent(client, workload_class="batch", tenant="demo-b"),
            ]
    else:
        client = GatewayClient(args.gateway_url, args.model)
        runs = [
            run_agent(
                client,
                question=args.question,
                tenant=args.tenant,
                workload_class="batch" if args.command == "batch" else "interactive",
            )
            for _ in range(args.jobs if args.command == "batch" else 1)
        ]
    return emit(runs, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
