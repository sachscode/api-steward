"""Helpers for the Part 5 notebook: scrape the gateway and Prometheus, read load and trace
files, run small live experiments, and save plots and metric dumps.

GATEWAY_URL and PROM_URL point at the cluster through SSH tunnels (see the notebook's first
cell). Everything here is read-only except the experiment helpers, which send requests
through the gateway like the app does.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import matplotlib.pyplot as plt
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8080").rstrip("/")
PROM = os.environ.get("PROM_URL", "http://127.0.0.1:9090").rstrip("/")
PLOTS = ROOT / "plots"
METRICS = ROOT / "metrics"

import sys

sys.path.insert(0, str(ROOT))
from app.tools import SYSTEM, TOOLS


# ---- gateway ---------------------------------------------------------------------------
def state() -> dict:
    return httpx.get(f"{GATEWAY}/debug/state", timeout=10).json()


def state_table() -> pd.DataFrame:
    rows = []
    for eid, e in state()["engines"].items():
        rows.append(
            {
                "engine": eid,
                "pool": e["pool"],
                "up": e["ok"],
                "warm": e["warm"],
                "slots open": f"{e['inflight']}/{e['slot_cap']}",
                "our queue (interactive)": e["our_queue"]["interactive"],
                "our queue (batch)": e["our_queue"]["batch"],
                "vLLM running": e["vllm_running"],
                "vLLM waiting": e["vllm_waiting"],
                "KV used": None if e["kv_usage"] is None else round(e["kv_usage"], 3),
                "preemptions": e["preemptions"],
            }
        )
    return pd.DataFrame(rows).set_index("engine")


def gateway_metrics() -> str:
    return httpx.get(f"{GATEWAY}/metrics", timeout=10).text


def counter_table(text: str, prefix: str) -> pd.DataFrame:
    """Parse orch_* Prometheus text lines starting with prefix into a table of labels + value."""
    rows = []
    for line in text.splitlines():
        if not line.startswith(prefix) or line.startswith("#"):
            continue
        name_labels, value = line.rsplit(" ", 1)
        labels = {}
        if "{" in name_labels:
            for part in name_labels.split("{", 1)[1].rstrip("}").split('",'):
                if "=" in part:
                    k, v = part.split("=", 1)
                    labels[k.strip()] = v.strip().strip('"')
        rows.append({**labels, "value": float(value)})
    return pd.DataFrame(rows)


# ---- prometheus ------------------------------------------------------------------------
def prom(query: str) -> pd.DataFrame:
    data = httpx.get(f"{PROM}/api/v1/query", params={"query": query}, timeout=20).json()
    return pd.DataFrame([{**r["metric"], "value": float(r["value"][1])} for r in data["data"]["result"]])


def prom_range(query: str, start: float, end: float, step: str = "5s", label: str | None = None) -> pd.DataFrame:
    """Wide frame: index = time, one column per series (named by `label` or the label set)."""
    data = httpx.get(
        f"{PROM}/api/v1/query_range", params={"query": query, "start": start, "end": end, "step": step}, timeout=30
    ).json()
    cols = {}
    for r in data["data"]["result"]:
        m = r["metric"]
        name = m.get(label, "") if label else ",".join(f"{k}={v}" for k, v in m.items() if k != "__name__") or "value"
        s = pd.Series({pd.to_datetime(t, unit="s"): float(v) for t, v in r["values"]})
        cols[name] = s if name not in cols else cols[name].add(s, fill_value=0)
    return pd.DataFrame(cols).sort_index()


# ---- run files -------------------------------------------------------------------------
def load_run(name: str) -> pd.DataFrame:
    """One row per agent job from metrics/load/<name>.jsonl."""
    return pd.read_json(METRICS / "load" / f"{name}.jsonl", lines=True)


def steps(jobs: pd.DataFrame) -> pd.DataFrame:
    """One row per agent step (model call) from a load run."""
    rows = []
    for job in jobs.itertuples():
        for s in job.steps:
            rows.append({"n": job.n, "phase": job.phase, "rate": job.rate, "klass": job.klass, "unique": job.unique, **s})
    return pd.DataFrame(rows)


def run_window(jobs: pd.DataFrame, pad_s: float = 30) -> tuple[float, float]:
    return float(jobs.start.min()) - pad_s, float((jobs.start + jobs.duration_s).max()) + pad_s


def traces() -> pd.DataFrame:
    raw = METRICS / "traces" / "cluster.jsonl"
    if raw.exists():
        return pd.read_json(raw, lines=True)
    # Fresh clone: the committed A100 extract (no prompts or hosts, so no per-hop detail).
    return pd.read_csv(METRICS / "a100-gateway-traces.csv.gz", low_memory=False)


# ---- live experiments ------------------------------------------------------------------
def step_payload(text: str, *, tenant: str = "demo-a", klass: str = "interactive", job: str | None = None,
                 max_tokens: int = 256) -> dict:
    return {
        "model": "steward",
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}],
        "tools": TOOLS,
        "max_tokens": max_tokens,
        "tenant": tenant,
        "workload_class": klass,
        "job_id": job or f"nb-{time.time_ns()}",
        "step": 1,
    }


async def _post_all(payloads: list[tuple[float, dict]]) -> list[dict]:
    """Send payloads, each after its own delay (seconds), concurrently; return responses."""
    async with httpx.AsyncClient(timeout=180) as client:

        async def one(delay: float, body: dict) -> dict:
            await asyncio.sleep(delay)
            t0 = time.time()
            r = await client.post(f"{GATEWAY}/v1/chat/completions", json=body)
            out = {"sent_at": t0, "status": r.status_code, "klass": body["workload_class"], "tenant": body["tenant"]}
            data = r.json()
            out.update(data.get("steward") or {})
            if r.status_code != 200:
                out["error"] = (data.get("error") or {}).get("code")
            return out

        return await asyncio.gather(*(one(d, b) for d, b in payloads))


def post_all(payloads: list[tuple[float, dict]]) -> pd.DataFrame:
    # Jupyter's kernel already runs an event loop, so run ours on its own thread.
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pd.DataFrame(pool.submit(asyncio.run, _post_all(payloads)).result())


def abandon_stream(text: str, after_chunks: int = 3) -> dict:
    """Start a streamed request and hang up after a few chunks, like a user closing the tab."""
    body = dict(step_payload(text, max_tokens=512), stream=True)
    got = 0
    with httpx.stream("POST", f"{GATEWAY}/v1/chat/completions", json=body, timeout=60) as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                got += 1
            if got >= after_chunks:
                break  # leaving the with-block closes the connection mid-stream
    return {"chunks_read": got}


# ---- saving evidence -------------------------------------------------------------------
def save(fig, name: str) -> Path:
    PLOTS.mkdir(parents=True, exist_ok=True)
    path = PLOTS / f"{name}.png"
    fig.savefig(path, dpi=130, bbox_inches="tight")
    return path


def dump(tag: str, **items) -> list[Path]:
    """Write evidence into metrics/<tag>-<name>.<ext>: text as .prom/.txt, others as JSON."""
    METRICS.mkdir(parents=True, exist_ok=True)
    out = []
    for name, value in items.items():
        if isinstance(value, str):
            path = METRICS / f"{tag}-{name}.prom"
            path.write_text(value)
        elif isinstance(value, pd.DataFrame):
            path = METRICS / f"{tag}-{name}.csv"
            value.to_csv(path)
        else:
            path = METRICS / f"{tag}-{name}.json"
            path.write_text(json.dumps(value, indent=2, default=str) + "\n")
        out.append(path)
    return out


def style() -> None:
    plt.rcParams.update({"figure.figsize": (10, 3.6), "axes.grid": True, "grid.alpha": 0.3, "axes.spines.top": False,
                         "axes.spines.right": False, "legend.frameon": False})
