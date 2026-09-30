"""The gateway box: an HTTP server in front of the engines.

  POST /v1/chat/completions   the serve path (stream or not)
  GET  /metrics               orch_* Prometheus metrics
  GET  /debug/state           our queues vs vLLM's queues, per engine, as JSON (Part 5)
  GET  /healthz, /v1/models

Client gone: for streams, Starlette cancels the response generator when the client
disconnects; for non-streams, a watcher polls the connection. Either way the cancellation
closes the upstream HTTP stream, vLLM aborts the request, and the engine frees its KV blocks.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .config import Settings, from_env
from .gateway import Gateway, GatewayRefusal
from .types import Shed


def create_app(settings: Settings | None = None, gateway: Gateway | None = None) -> FastAPI:
    gw = gateway or Gateway(settings or from_env())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await gw.start()
        yield
        await gw.stop()

    app = FastAPI(title="API Steward gateway", lifespan=lifespan)
    app.state.gateway = gw

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse(Shed(400, "invalid_json", "guardrails").body(), status_code=400)
        if isinstance(payload, dict) and payload.get("stream"):
            return await _stream(gw, payload)
        task = asyncio.create_task(gw.complete(payload))
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=0.25)
            if not done and await request.is_disconnected():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                return Response(status_code=499)
        status, body, headers = task.result()
        return JSONResponse(body, status_code=status, headers=headers)

    @app.get("/metrics")
    async def metrics():
        return Response(generate_latest(gw.metrics.registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/debug/state")
    async def debug_state():
        return gw.state()

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "engines": {e: s.ok for e, s in gw.fleet.states.items()}}

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [{"id": gw.settings.served_model, "object": "model"}]}

    return app


async def _stream(gw: Gateway, payload: dict):
    planned = gw.plan(payload)
    if isinstance(planned, Shed):
        return JSONResponse(planned.body(), status_code=planned.status)
    if isinstance(planned, tuple):
        status, body, headers = await gw._refused(*planned, None)
        return JSONResponse(body, status_code=status, headers=headers)

    async def events():
        try:
            async for chunk in gw.run(planned):
                yield f"data: {json.dumps(chunk)}\n\n"
            gw._write_trace(planned.req, planned.trace, 200, "local")
        except GatewayRefusal as refusal:
            gw._write_trace(planned.req, planned.trace, refusal.shed.status, "local")
            yield f"data: {json.dumps(refusal.shed.body())}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"X-Via": "local"})
