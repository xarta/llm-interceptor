"""
llm-interceptor: lightweight async proxy between Caddy and LiteLLM.

For requests whose path matches defaults.inject_paths in config.yaml, the body
is inspected and per-model transforms are applied (e.g. inject_no_think).
All other requests and paths are passed through transparently, including SSE
streaming responses.

Config is loaded at startup and reloaded on SIGHUP (docker kill -s HUP llm-interceptor)
or by POST /_interceptor/reload from inside the Docker network.

config.yaml shape:
  defaults:
    inject_token: "/no_think"       # string prepended to the first user message
    inject_paths:                   # paths on which body inspection runs
      - /v1/chat/completions
      - /v1/completions
  models:
    "my-model-name":
      inject_no_think: true
      inject_token: "/no_think"     # optional per-model override
"""

import json
import logging
import os
import signal
import sys

import httpx
import yaml
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LITELLM_URL = os.environ.get("LITELLM_URL", "http://litellm:4000").rstrip("/")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.yaml")
LOG_LEVEL   = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    stream=sys.stdout,
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [interceptor] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def _read_config() -> dict:
    try:
        with open(CONFIG_PATH) as fh:
            data = yaml.safe_load(fh) or {}
        log.info("Config loaded from %s — %d model rule(s)", CONFIG_PATH, len(data.get("models", {})))
        return data
    except FileNotFoundError:
        log.warning("Config file not found at %s — running with empty config", CONFIG_PATH)
        return {}
    except Exception as exc:
        log.error("Failed to load config: %s — keeping previous config", exc)
        return _CFG  # keep old copy if we have one


def _apply_log_level(cfg: dict) -> None:
    """Set log level from defaults.debug in config (hot-reloadable via SIGHUP)."""
    debug = cfg.get("defaults", {}).get("debug", False)
    level = logging.DEBUG if debug else getattr(logging, LOG_LEVEL, logging.INFO)
    log.setLevel(level)
    if debug:
        log.info("Debug logging ENABLED via config")


_CFG: dict = _read_config()
_apply_log_level(_CFG)


def reload_config(*_):
    global _CFG
    _CFG = _read_config()
    _apply_log_level(_CFG)


def get_model_rule(model_name: str) -> dict:
    """Return the rule dict for a model, or {} if not configured."""
    return _CFG.get("models", {}).get(model_name, {})


_DEFAULT_INJECT_PATHS = {"/v1/chat/completions", "/v1/completions"}
_DEFAULT_INJECT_TOKEN = "/no_think"


def get_inject_paths() -> set:
    """Paths on which body inspection runs (from defaults.inject_paths)."""
    paths = _CFG.get("defaults", {}).get("inject_paths")
    if paths:
        return set(paths)
    return _DEFAULT_INJECT_PATHS


def get_inject_token(rule: dict) -> str:
    """Inject token: per-model override > defaults.inject_token > built-in default."""
    if "inject_token" in rule:
        return rule["inject_token"]
    return _CFG.get("defaults", {}).get("inject_token", _DEFAULT_INJECT_TOKEN)


# Reload config on SIGHUP (docker kill -s HUP llm-interceptor)
signal.signal(signal.SIGHUP, reload_config)

# ---------------------------------------------------------------------------
# Request transforms
# ---------------------------------------------------------------------------

def _prepend_token(messages: list, token: str) -> list:
    """
    Prepend `token` to the content of the FIRST user message only.
    Handles both plain string content and multi-part (list) content.
    Idempotent — won't double-inject if the token is already present.
    """
    result = []
    injected = False
    for msg in messages:
        if not injected and msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                if not content.startswith(token):
                    msg = {**msg, "content": token + content}
            elif isinstance(content, list):
                new_parts = []
                part_injected = False
                for part in content:
                    if not part_injected and part.get("type") == "text":
                        text = part.get("text", "")
                        if not text.startswith(token):
                            part = {**part, "text": token + text}
                        part_injected = True
                    new_parts.append(part)
                msg = {**msg, "content": new_parts}
            injected = True
        result.append(msg)
    return result


def _is_minimax_model(model_name: str) -> bool:
    """Return True if model_name looks like a MiniMax model (case-insensitive)."""
    lower = model_name.lower()
    return lower.startswith("minimax") or lower.startswith("minimax/")


def _filter_ghost_tool_calls(messages: list) -> tuple[list, int, int]:
    """
    Walk *messages* in order and:
      1. Strip ghost tool_calls from assistant messages.
         A ghost is any tool_call with an empty ``id`` or empty ``function.name``.
      2. Strip orphaned tool results whose ``tool_call_id`` no longer matches
         a valid tool_call in the preceding assistant message.

    Returns (cleaned_messages, ghosts_stripped, orphans_stripped).
    The original list is never mutated.
    """
    result: list = []
    current_valid_ids: set | None = None  # None = no assistant tracked yet
    ghosts_stripped = 0
    orphans_stripped = 0

    for msg in messages:
        role = msg.get("role")

        if role == "assistant" and msg.get("tool_calls"):
            original = msg["tool_calls"]
            cleaned = [
                tc for tc in original
                if tc.get("id") and tc.get("function", {}).get("name")
            ]
            current_valid_ids = {tc["id"] for tc in cleaned}
            removed = len(original) - len(cleaned)

            if removed:
                ghosts_stripped += removed
                msg = dict(msg)  # shallow copy — don't mutate caller's data
                if cleaned:
                    msg["tool_calls"] = cleaned
                else:
                    msg.pop("tool_calls", None)

            result.append(msg)

        elif role == "tool":
            tcid = msg.get("tool_call_id")
            if current_valid_ids is None:
                # No preceding assistant tool_calls tracked — pass through
                # (safety: don't drop tool results that belong to messages
                # outside our window, e.g. the very first message).
                result.append(msg)
            elif tcid and tcid in current_valid_ids:
                result.append(msg)
            else:
                orphans_stripped += 1
                # ghost / orphaned tool result — skip

        else:
            # system, user, etc. — pass through and reset tracking
            current_valid_ids = None
            result.append(msg)

    return result, ghosts_stripped, orphans_stripped


def maybe_transform_body(path: str, raw_body: bytes) -> bytes:
    """
    Inspect and optionally transform the request body.
    Returns the (possibly modified) body bytes.
    Only runs for paths listed in defaults.inject_paths (or the built-in defaults).
    """
    if path not in get_inject_paths() or not raw_body:
        return raw_body

    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError:
        return raw_body  # can't parse → pass through

    model = data.get("model", "")
    modified = False

    # --- MiniMax ghost tool-call filter (pattern-based, no config needed) ---
    if _is_minimax_model(model) and "messages" in data:
        log.debug("[minimax-ghost-filter] Scanning %d message(s) for model=%r", len(data["messages"]), model)
        cleaned, ghosts, orphans = _filter_ghost_tool_calls(data["messages"])
        if ghosts or orphans:
            data["messages"] = cleaned
            modified = True
            log.info(
                "[minimax-ghost-filter] model=%r — stripped %d ghost tool_call(s), "
                "%d orphaned tool result(s)",
                model, ghosts, orphans,
            )

    # --- Config-driven per-model transforms ---
    rule = get_model_rule(model)

    if rule.get("inject_no_think") and "messages" in data:
        token = get_inject_token(rule)
        data["messages"] = _prepend_token(data["messages"], token)
        modified = True
        log.debug("Injected token=%r for model=%r", token, model)

    if rule.get("model_rename"):
        new_name = rule["model_rename"]
        data["model"] = new_name
        modified = True
        log.debug("Renamed model %r -> %r", model, new_name)

    return json.dumps(data, separators=(",", ":")).encode() if modified else raw_body


# ---------------------------------------------------------------------------
# HTTP client (singleton, connection-pooled)
# ---------------------------------------------------------------------------

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=LITELLM_URL,
            timeout=httpx.Timeout(connect=30.0, read=600.0, write=600.0, pool=30.0),
            limits=httpx.Limits(max_connections=512, max_keepalive_connections=128),
            follow_redirects=False,
        )
    return _client


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

# Internal reload endpoint (no auth — only reachable inside the Docker network)
async def handle_reload(request: Request) -> Response:
    reload_config()
    return Response("OK\n", media_type="text/plain")


async def proxy(request: Request) -> Response:
    path = request.url.path
    query = request.url.query
    url = path + (f"?{query}" if query else "")

    raw_body = await request.body()
    body = maybe_transform_body(path, raw_body)

    # Forward all headers except hop-by-hop ones; let httpx set content-length
    skip = {"host", "content-length", "transfer-encoding", "connection", "keep-alive",
            "proxy-authenticate", "proxy-authorization", "te", "trailers", "upgrade"}
    headers = {k: v for k, v in request.headers.items() if k.lower() not in skip}

    upstream_req = get_client().build_request(
        method=request.method,
        url=url,
        headers=headers,
        content=body,
    )

    upstream_resp = await get_client().send(upstream_req, stream=True)

    # Strip hop-by-hop response headers
    resp_headers = {
        k: v for k, v in upstream_resp.headers.items()
        if k.lower() not in {"transfer-encoding", "connection"}
    }

    return StreamingResponse(
        upstream_resp.aiter_bytes(chunk_size=None),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=upstream_resp.headers.get("content-type"),
        background=None,
    )


app = Starlette(routes=[
    Route("/_interceptor/reload", handle_reload, methods=["POST"]),
    Route("/{path:path}", proxy, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"]),
])

log.info("llm-interceptor starting — upstream: %s", LITELLM_URL)
