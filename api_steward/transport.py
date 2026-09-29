"""Small bounded JSON transport shared by the gateway and application."""

import json
import re
from urllib import error, request
from urllib.parse import urlsplit

MAX_RESPONSE_BYTES = 262_144
STAGES = ["guardrails", "admit", "place", "queue", "worker"]


class ServeError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def endpoint(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Expected an HTTP(S) base URL without credentials, query or fragment")
    return base_url.rstrip("/") + "/v1/chat/completions"


def post_json(url: str, body: dict, timeout: float, api_key: str = "") -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    # Disable ambient proxy routing and redirects for source-containing requests.
    opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except error.HTTPError as exc:
        code = "http_request_rejected"
        try:
            detail = json.loads(exc.read(MAX_RESPONSE_BYTES))
            candidate = detail.get("error") if isinstance(detail, dict) else None
            if isinstance(candidate, str) and re.fullmatch(r"[a-z_]{1,80}", candidate):
                code = candidate
        except (ValueError, OSError):
            pass
        finally:
            exc.close()
        raise ServeError(exc.code, code) from None
    except (OSError, error.URLError):
        raise ServeError(503, "endpoint_unavailable") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ServeError(502, "response_too_large")
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ServeError(502, "invalid_response_json") from None
    if not isinstance(result, dict):
        raise ServeError(502, "invalid_response_shape")
    return result
