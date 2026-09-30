"""Gateway settings, read once from the environment.

ENGINES lists every vLLM engine the gateway may use, as id=url pairs. The id prefix names
the pool: prefill-* engines produce KV, decode-* engines consume it and stream tokens.

    ENGINES="prefill-0=http://10.19.54.103:8000,decode-0=http://10.19.54.103:8001"
    TENANTS="demo-a:40000:8:yes,demo-b:40000:8:no"   # name:tokens_per_min:max_inflight:overflow
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class EngineSpec:
    id: str
    url: str

    @property
    def pool(self) -> str:
        return "prefill" if self.id.startswith("prefill") else "decode"


@dataclass(frozen=True)
class TenantPolicy:
    tokens_per_min: int
    max_inflight: int
    allow_overflow: bool


@dataclass
class OverflowSettings:
    base_url: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)
    rps: float = 0.5
    burst: int = 2
    max_inflight: int = 2
    max_tokens: int = 256
    timeout_s: float = 30.0

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.model and self.api_key)


@dataclass
class Settings:
    engines: list[EngineSpec]
    tenants: dict[str, TenantPolicy]
    served_model: str = "steward"
    # Concurrent requests the gateway dispatches to one engine. Prefill requests are short
    # (max_tokens=1) so a small cap keeps vLLM's own waiting queue near zero; decode can hold
    # many sequences, so its cap sits below the engine's max_num_seqs.
    slot_cap: dict[str, int] = field(default_factory=lambda: {"prefill": 8, "decode": 64})
    # Below this many estimated prompt tokens the gateway skips the KV hop and lets the
    # decode engine prefill locally: moving KV would cost more than recomputing it.
    hop_min_tokens: int = 512
    kv_bytes_per_token: int = 98_304  # Qwen3-30B-A3B BF16: 2 x 48 x 4 x 128 x 2
    block_size: int = 16
    kv_high_water: dict[str, float] = field(default_factory=lambda: {"interactive": 0.92, "batch": 0.80})
    deadline_s: dict[str, float] = field(default_factory=lambda: {"interactive": 30.0, "batch": 120.0})
    batch_aging_s: float = 15.0
    scrape_interval_s: float = 0.5
    stale_s: float = 2.0
    prefill_policy: str = "prefix_then_load"
    decode_policy: str = "p2c"
    ttft_slo_s: float = 2.0
    warmup_requests: int = 3
    ramp_weights: tuple[float, ...] = (0.25, 0.5, 1.0)
    ramp_step_s: float = 15.0
    # An engine process older than this is treated as warm when the gateway (re)starts: the
    # ramp protects engines that just started, not ones the gateway merely forgot about.
    warm_uptime_s: float = 300.0
    max_context_chars: int = 48_000
    max_steps: int = 8
    max_output_tokens: int = 512
    trace_path: str = ""
    overflow: OverflowSettings = field(default_factory=OverflowSettings)

    def tenant(self, name: str) -> TenantPolicy | None:
        return self.tenants.get(name)


def _engines(raw: str) -> list[EngineSpec]:
    out = []
    for part in filter(None, (p.strip() for p in raw.split(","))):
        ident, _, url = part.partition("=")
        if not url or not ident.startswith(("prefill", "decode")):
            raise ValueError(f"ENGINES entry {part!r} must be prefill-N=URL or decode-N=URL")
        out.append(EngineSpec(ident.strip(), url.strip().rstrip("/")))
    return out


def _tenants(raw: str) -> dict[str, TenantPolicy]:
    out = {}
    for part in filter(None, (p.strip() for p in raw.split(","))):
        name, tpm, inflight, overflow = (part.split(":") + ["no"])[:4]
        out[name] = TenantPolicy(int(tpm), int(inflight), overflow.lower() in ("yes", "true", "1"))
    return out


def from_env(env: dict[str, str] | None = None) -> Settings:
    e = os.environ if env is None else env
    s = Settings(
        engines=_engines(e.get("ENGINES", "")),
        tenants=_tenants(e.get("TENANTS", "demo-a:40000:8:yes,demo-b:40000:8:yes,sweep:80000:16:yes")),
        served_model=e.get("SERVED_MODEL", "steward"),
        hop_min_tokens=int(e.get("HOP_MIN_TOKENS", "512")),
        kv_bytes_per_token=int(e.get("KV_BYTES_PER_TOKEN", "98304")),
        prefill_policy=e.get("PREFILL_POLICY", "prefix_then_load"),
        decode_policy=e.get("DECODE_POLICY", "p2c"),
        ttft_slo_s=float(e.get("TTFT_SLO_S", "2.0")),
        trace_path=e.get("TRACE_PATH", ""),
    )
    s.slot_cap = {"prefill": int(e.get("PREFILL_SLOT_CAP", "8")), "decode": int(e.get("DECODE_SLOT_CAP", "64"))}
    s.overflow = OverflowSettings(
        base_url=e.get("OVERFLOW_BASE_URL", "").rstrip("/"),
        model=e.get("OVERFLOW_MODEL", ""),
        api_key=e.get("OVERFLOW_API_KEY", ""),
        rps=float(e.get("OVERFLOW_RPS", "0.5")),
        burst=int(e.get("OVERFLOW_BURST", "2")),
        max_inflight=int(e.get("OVERFLOW_MAX_INFLIGHT", "2")),
        max_tokens=int(e.get("OVERFLOW_MAX_TOKENS", "256")),
    )
    return s
