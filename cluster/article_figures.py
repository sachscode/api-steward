"""Figures for the article, built only from committed evidence in metrics/.

    .venv/bin/python cluster/article_figures.py      # writes plots/article-*.png

Palette: validated categorical pair (blue = local/colocated, orange = hop/split) on a light
surface; text in ink tokens, never series colours.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
M, OUT = ROOT / "metrics", ROOT / "plots"
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e1"
BLUE, ORANGE, BLUE_LIGHT = "#2a78d6", "#eb6834", "#9ec5f4"

plt.rcParams.update(
    {
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "font.family": "DejaVu Sans",
        "font.size": 11,
        "text.color": INK,
        "axes.labelcolor": INK2,
        "xtick.color": INK2,
        "ytick.color": INK2,
        "axes.edgecolor": GRID,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "grid.color": GRID,
        "grid.linewidth": 0.8,
        "axes.axisbelow": True,
        "legend.frameon": False,
    }
)


def title(ax, main: str, sub: str) -> None:
    ax.set_title(main, loc="left", fontsize=14, fontweight="bold", color=INK, pad=26)
    ax.text(0, 1.035, sub, transform=ax.transAxes, fontsize=10.5, color=INK2, va="bottom")


def save(fig, name: str) -> None:
    OUT.mkdir(exist_ok=True)
    fig.savefig(OUT / f"article-{name}.png", dpi=180, bbox_inches="tight")
    plt.close(fig)
    print("wrote", OUT / f"article-{name}.png")


def hop_vs_local() -> None:
    rows = json.loads((M / "a100-hop-probe.json").read_text())
    sizes = sorted({r["target_tokens"] for r in rows})
    tok = [statistics.median(r["prompt_tokens"] for r in rows if r["target_tokens"] == s) for s in sizes]
    hop = [statistics.median(r["hop_total_s"] for r in rows if r["target_tokens"] == s) * 1000 for s in sizes]
    loc = [statistics.median(r["local_first_token_s"] for r in rows if r["target_tokens"] == s) * 1000 for s in sizes]
    fig, ax = plt.subplots(figsize=(8.5, 4.6))
    ax.plot(tok, hop, color=ORANGE, lw=2, marker="o", ms=8, mec=SURFACE, mew=2)
    ax.plot(tok, loc, color=BLUE, lw=2, marker="o", ms=8, mec=SURFACE, mew=2)
    ax.annotate(
        "Hop: prefill engine + NIXL transfer + decode",
        (tok[-1], hop[-1]),
        xytext=(-8, 10),
        textcoords="offset points",
        ha="right",
        color=INK,
        fontsize=10,
    )
    ax.annotate(
        "Local: decode engine computes the prompt",
        (tok[3], loc[3]),
        xytext=(12, -30),
        textcoords="offset points",
        ha="left",
        color=INK,
        fontsize=10,
    )
    ax.set_xlabel("Prompt length (tokens)")
    ax.set_ylabel("Time to first token (ms)")
    ax.set_ylim(0, max(hop) * 1.15)
    title(
        ax,
        "For fresh prompts, the full split path was slower at every size",
        "Hop = remote prefill + KV transfer (~4.5 GB/s, RoCE NIC loopback on one host) + first token. Median of 3, no contention.",
    )
    save(fig, "1-hop-vs-recompute")


def _started_rates(run: str) -> list[float]:
    """Jobs/s the load generator actually started per step (it lags its target when its threads are busy)."""
    import pandas as pd

    jobs = pd.read_json(M / "load" / f"a100-{run}.jsonl", lines=True)
    g = jobs.groupby("phase").start
    return list(g.count() / (g.max() - g.min()))


def split_vs_colocated() -> None:
    steps = {r: json.loads((M / f"a100-{r}-analysis.json").read_text())["steps"] for r in ("sweep3", "colo")}
    rates = [s["rate"] for s in steps["colo"]]
    split = [s["ttft_p99"] for s in steps["sweep3"]]
    colo = [s["ttft_p99"] for s in steps["colo"]]
    fig, ax = plt.subplots(figsize=(8.5, 4.6))
    w = 0.36
    xs = range(len(rates))
    b1 = ax.bar([x - w / 2 - 0.01 for x in xs], split, w, color=ORANGE, label="Prefill/decode split (4 + 4 GPUs)")
    b2 = ax.bar([x + w / 2 + 0.01 for x in xs], colo, w, color=BLUE, label="Two colocated engines (4 + 4 GPUs)")
    for bars in (b1, b2):
        for b in bars:
            ax.text(
                b.get_x() + b.get_width() / 2,
                b.get_height() + 0.3,
                f"{b.get_height():.1f} s",
                ha="center",
                va="bottom",
                fontsize=10,
                color=INK,
            )
    started = {r: _started_rates(r) for r in ("sweep3", "colo")}
    ax.set_xticks(
        list(xs),
        [f"target {r:g}/s\nstarted {a:.1f} / {b:.1f}" for r, a, b in zip(rates, started["sweep3"], started["colo"])],
    )
    ax.set_ylabel("Time to first token, p99 (s)")
    ax.set_ylim(0, 21)
    ax.legend(loc="upper right")
    title(
        ax,
        "Same 8 GPUs and target load: colocated held the tail",
        "p99 TTFT per model call at the gateway. Started rates (split / colocated): the load generator fell behind at 28.",
    )
    save(fig, "2-split-vs-colocated")


def bottleneck_shift() -> None:
    res = {r: json.loads((M / f"a100-{r}-analysis.json").read_text())["resources"] for r in ("sweep2", "sweep3")}

    def gpu(r: str, ids: range) -> float:
        g = res[r]["GPU util avg (%)"]
        return statistics.mean(g[f"gpu{i}"] for i in ids)

    labels = ["Prefill GPUs busy", "Decode GPUs busy", "vLLM decode sequences\n(of max_num_seqs 256)", "KV cache used"]
    before = [
        gpu("sweep2", range(4)),
        gpu("sweep2", range(4, 8)),
        100 * res["sweep2"]["peak vLLM running"]["vllm-decode-0"] / 256,
        100 * res["sweep2"]["peak KV used (0-1)"]["vllm-decode-0"],
    ]
    after = [
        gpu("sweep3", range(4)),
        gpu("sweep3", range(4, 8)),
        100 * res["sweep3"]["peak vLLM running"]["vllm-decode-0"] / 256,
        100 * res["sweep3"]["peak KV used (0-1)"]["vllm-decode-0"],
    ]
    fig, ax = plt.subplots(figsize=(8.5, 4.6))
    ys = range(len(labels))
    h = 0.36
    ax.barh([y - h / 2 - 0.01 for y in ys], before, h, color=ORANGE, label="Gateway prefill cap = 8")
    ax.barh([y + h / 2 + 0.01 for y in ys], after, h, color=BLUE, label="Gateway prefill cap = 32")
    for y, v in zip(ys, before, strict=True):
        ax.text(v + 1, y - h / 2, f"{v:.0f}%", va="center", fontsize=10, color=INK)
    for y, v in zip(ys, after, strict=True):
        ax.text(v + 1, y + h / 2, f"{v:.0f}%", va="center", fontsize=10, color=INK)
    ax.set_yticks(list(ys), labels)
    ax.invert_yaxis()
    ax.set_xlim(0, 110)
    ax.set_xlabel("Peak or average use (%)")
    ax.grid(axis="y", visible=False)
    ax.legend(loc="lower right")
    title(
        ax,
        "The first bottleneck was a gateway setting",
        "GPU busy = run averages (the cap-8 run also had two lighter load steps); sequences and KV = peaks.",
    )
    save(fig, "3-bottleneck-shift")


def scale_out_timeline() -> None:
    # Seconds from the start of the 20 jobs/s burst (metrics/a100-autoscale-*, Kubernetes events).
    burst_end, decided, started, ready, removed = 156, 21, 23, 239, 381
    fig, ax = plt.subplots(figsize=(8.5, 3.2))
    ax.barh(1, burst_end, left=0, height=0.5, color=ORANGE)
    ax.text(
        (decided + burst_end) / 2 + 5,
        1,
        "Traffic burst: 20 agent jobs/s",
        ha="center",
        va="center",
        color=SURFACE,
        fontsize=10,
    )
    ax.barh(0, ready - started, left=started, height=0.5, color=BLUE_LIGHT)
    ax.text(
        (started + ready) / 2,
        0,
        "New replica loading weights, compiling",
        ha="center",
        va="center",
        color=INK,
        fontsize=10,
    )
    ax.barh(0, removed - ready, left=ready, height=0.5, color=BLUE)
    ax.text((ready + removed) / 2, 0, "Ready, idle", ha="center", va="center", color=SURFACE, fontsize=10)
    ax.axvline(decided, color=INK2, lw=1, ls="--")
    ax.text(decided + 3, 1.42, "KEDA scales up at +21 s", color=INK, fontsize=10)
    ax.set_yticks([0, 1], ["Replica 2", "Load"])
    ax.set_ylim(-0.5, 1.7)
    ax.set_xlim(0, 400)
    ax.set_xlabel("Seconds from the start of the burst")
    ax.grid(axis="y", visible=False)
    title(
        ax,
        "Autoscaling reacted in 21 s; the replica arrived 83 s too late",
        "A 30B MoE replica took ~3.6 min to become ready and served none of the burst. Times: recorded k8s events.",
    )
    save(fig, "4-scale-out-timeline")


def _trace_by_step(run: str):
    """Served requests from the gateway trace, tagged with the load step they arrived in.

    Step boundaries come from the load file (each job records its step), not from the planned
    durations: under overload the load generator itself fell behind, so real steps ran long."""
    import pandas as pd

    jobs = pd.read_json(M / "load" / f"a100-{run}.jsonl", lines=True)
    bounds = jobs.groupby("phase").agg(rate=("rate", "first"), first=("start", "min")).sort_index()
    end = float((jobs.start + jobs.duration_s).max())
    edges = list(bounds["first"]) + [end]
    # Committed extract of metrics/traces/cluster.jsonl (no prompts, no hosts), so a fresh clone can rebuild charts 5-6.
    t = pd.read_csv(M / "a100-gateway-traces.csv.gz", low_memory=False)
    t["arrival"] = t.ts - t.e2e_s.fillna(0)
    w = t[(t.arrival >= edges[0]) & (t.arrival <= end) & (t.status == 200)].copy()
    w["load_step"] = pd.cut(w.arrival, edges, labels=list(bounds["rate"]), include_lowest=True)
    return w


def ttft_breakdown() -> None:
    """Mean TTFT per load step, split into where the time went."""
    parts = {
        "Waiting in the gateway queue": ("#52514e", lambda w: w.queue_wait_decode_s + w.queue_wait_prefill_s.fillna(0)),
        "Prefill on the prefill engine": (ORANGE, lambda w: w.prefill_s.fillna(0)),
        "KV transfer + first decode token": (
            "#f4a582",
            # These traces predate the gateway fix that moved the engine clock past the prefill
            # queue, so their engine_ttft_s still includes that wait; subtract it here.
            lambda w: (w.engine_ttft_s - w.queue_wait_prefill_s.fillna(0) - w.prefill_s.fillna(0)).where(
                w.hop_mode == "nixl", 0
            ),
        ),
        "Engine computes prompt + first token": (BLUE, lambda w: w.engine_ttft_s.where(w.hop_mode != "nixl", 0)),
    }
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.6), sharey=True)
    for ax, (run, label) in zip(
        axes, (("sweep3", "Prefill/decode split"), ("colo", "Two colocated engines")), strict=True
    ):
        w = _trace_by_step(run)
        steps = list(w.load_step.cat.categories)
        bottom = [0.0] * len(steps)
        for name, (color, fn) in parts.items():
            vals = [float(fn(w[w.load_step == s]).clip(lower=0).mean()) for s in steps]
            if not any(v > 0.005 for v in vals):
                continue
            ax.bar(
                [f"{s:g}/s" for s in steps],
                vals,
                0.6,
                bottom=bottom,
                color=color,
                edgecolor=SURFACE,
                linewidth=2,
                label=name,
            )
            bottom = [b + v for b, v in zip(bottom, vals, strict=True)]
        for i, total in enumerate(bottom):
            ax.text(i, total + 0.06, f"{total:.2f} s", ha="center", va="bottom", fontsize=10, color=INK)
        ax.set_title(label, fontsize=11.5, color=INK, loc="left")
        ax.set_xlabel("Target load (agent jobs/s)")
        ax.grid(axis="x", visible=False)
    axes[0].set_ylabel("Mean time to first token (s)")
    handles = {h.get_label(): h for ax in axes for h in ax.containers}
    fig.legend(list(handles.values()), list(handles.keys()), loc="lower center", ncol=2, bbox_to_anchor=(0.5, -0.16))
    fig.suptitle(
        "Where time to first token goes as load rises",
        x=0.06,
        ha="left",
        fontsize=14,
        fontweight="bold",
        color=INK,
        y=1.04,
    )
    fig.text(
        0.06,
        0.965,
        "Same 8 GPUs and target load. Under the split, the extra time at the 28/s target is mostly the gateway's own queue.",
        fontsize=10.5,
        color=INK2,
    )
    save(fig, "5-ttft-breakdown")


def ttft_cdf() -> None:
    """TTFT distribution by class at the top load step: the batch aging ceiling."""
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.4), sharey=True)
    for ax, (run, label) in zip(
        axes, (("sweep3", "Prefill/decode split"), ("colo", "Two colocated engines")), strict=True
    ):
        w = _trace_by_step(run)
        top = w[w.load_step == w.load_step.cat.categories[-1]]
        for klass, color in (("interactive", BLUE), ("batch", ORANGE)):
            v = sorted(top[top.klass == klass].ttft_s.dropna())
            ax.plot(v, [(i + 1) / len(v) for i in range(len(v))], color=color, lw=2, label=klass.capitalize())
        ax.axvline(15, color=INK2, lw=1, ls="--")
        ax.text(15.3, 0.08, "batch promoted\nafter 15 s", fontsize=9.5, color=INK2)
        ax.set_xlim(0, 20)
        ax.set_title(label, fontsize=11.5, color=INK, loc="left")
        ax.set_xlabel("Time to first token (s)")
    axes[0].set_ylabel("Share of requests")
    axes[0].legend(loc="lower right", bbox_to_anchor=(1.0, 0.22))
    fig.suptitle(
        "Priority under overload: interactive is protected, batch waits",
        x=0.06,
        ha="left",
        fontsize=14,
        fontweight="bold",
        color=INK,
        y=1.04,
    )
    fig.text(
        0.06,
        0.965,
        "28 agent jobs/s. Batch requests that queue are held until their 15 s aging promotion.",
        fontsize=10.5,
        color=INK2,
    )
    save(fig, "6-ttft-cdf-by-class")


if __name__ == "__main__":
    hop_vs_local()
    split_vs_colocated()
    bottleneck_shift()
    scale_out_timeline()
    ttft_breakdown()
    ttft_cdf()
