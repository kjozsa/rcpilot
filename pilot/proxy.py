"""
Transparent HTTP proxy for Anthropic API calls.

Sits between Claude Code and api.anthropic.com. Captures:
  - Rate-limit window utilization (anthropic-ratelimit-unified-{window}-utilization headers)
  - Per-model call counts (from request body `model` field)

Stats are kept in memory and served via /api/stats.
All captured data is global (not per-session) — it resets on service restart.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from loguru import logger

_UPSTREAM = "https://api.anthropic.com"
_RATELIMIT_PREFIX = "anthropic-ratelimit-unified-"

_stats: dict[str, Any] = {
    "windows": {},    # window_name → utilization float 0–1, e.g. {"5h": 0.45, "7d": 0.72}
    "resets": {},     # window_name → ISO reset timestamp, e.g. {"5h": "2026-04-11T02:00:00Z"}
    "models": {},     # model_name → call count, e.g. {"claude-opus-4-6": 12}
    "updated_at": None,
}
_stats_lock = threading.Lock()


def get_stats() -> dict[str, Any]:
    with _stats_lock:
        return dict(_stats)


def _update_stats(model: str | None, response_headers: httpx.Headers) -> None:
    windows: dict[str, float] = {}
    resets: dict[str, str] = {}
    for k, v in response_headers.items():
        k_lower = k.lower()
        if not k_lower.startswith(_RATELIMIT_PREFIX):
            continue
        suffix = k_lower[len(_RATELIMIT_PREFIX):]
        # suffix looks like "5h-utilization" or "7d-utilization"
        if suffix.endswith("-utilization"):
            window_name = suffix[: -len("-utilization")]
            try:
                windows[window_name] = float(v)
            except ValueError:
                pass
        # suffix looks like "5h-requests-reset" or "5h-tokens-reset" → Unix timestamp
        elif suffix.endswith("-reset"):
            parts = suffix[: -len("-reset")].split("-", 1)
            window_name = parts[0]   # "5h", "7d", etc.
            if window_name not in resets:
                try:
                    ts = int(v)
                    resets[window_name] = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                except (ValueError, OSError):
                    resets[window_name] = v

    with _stats_lock:
        if windows:
            _stats["windows"].update(windows)
        if resets:
            _stats["resets"].update(resets)
        if model:
            _stats["models"][model] = _stats["models"].get(model, 0) + 1
        _stats["updated_at"] = datetime.now(timezone.utc).isoformat()


_STRIP_REQ_HEADERS = {"host", "content-length"}
_STRIP_RESP_HEADERS = {"content-encoding", "transfer-encoding", "content-length"}


async def handle(request: Request, path: str) -> StreamingResponse:
    body = await request.body()

    model: str | None = None
    if body:
        try:
            model = json.loads(body).get("model")
        except Exception:
            pass

    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _STRIP_REQ_HEADERS
    }

    url = f"{_UPSTREAM}/{path}"
    if request.url.query:
        url += f"?{request.url.query}"

    client = httpx.AsyncClient(timeout=httpx.Timeout(300.0))
    try:
        upstream_req = client.build_request(request.method, url, headers=headers, content=body)
        resp = await client.send(upstream_req, stream=True)
    except httpx.RequestError as exc:
        await client.aclose()
        logger.warning("proxy: upstream request failed: {}", exc)
        raise HTTPException(status_code=502, detail=f"Upstream error: {exc}")

    _update_stats(model, resp.headers)
    logger.debug("proxy: {} {} → {} (model={})", request.method, path, resp.status_code, model)

    forward_headers = {
        k: v for k, v in resp.headers.items()
        if k.lower() not in _STRIP_RESP_HEADERS
    }

    async def body_stream():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        body_stream(),
        status_code=resp.status_code,
        headers=forward_headers,
        media_type=resp.headers.get("content-type"),
    )
