"""Native Hermes plugin. Stdlib only; the service has its own isolated venv."""

import functools
import json
import logging
import os
import re
import stat
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)


def _request(method: str, endpoint: str, payload: dict | None = None) -> str:
    path = Path(
        os.environ.get(
            "HERMES_CALL_CONFIG",
            str(Path.home() / ".config/hermes-call/config.json"),
        )
    )
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("hermes-call config requires chmod 600")
    config = json.loads(path.read_text(encoding="utf-8"))
    port = config.get("api_port", 8765)
    token = config.get("api_token")
    if type(port) is not int or not 1024 <= port <= 65535 or not isinstance(token, str):
        raise ValueError("invalid hermes-call API configuration")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{endpoint}",
        method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )

    # No proxies or redirects: credentials must never leave loopback.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=70) as response:
        body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise ValueError("hermes-call response too large")
        result = json.loads(body)
        # Dialing confirms PBX submission only. It does not confirm conversation delivery.
        return json.dumps(result, ensure_ascii=False)


def _safe_handler(func):
    @functools.wraps(func)
    def wrapped(args: dict, **kwargs) -> str:
        try:
            if not isinstance(args, dict):
                raise ValueError("arguments must be an object")
            return func(args, **kwargs)
        except urllib.error.HTTPError as exc:
            return json.dumps(
                {
                    "error": "telephone service rejected the request",
                    "status": exc.code,
                    "retry": "reuse the same request_id",
                }
            )
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            log.error("hermes-call tool failed (%s)", type(exc).__name__)
            return json.dumps(
                {
                    "error": "telephone service unavailable or misconfigured",
                    "retry": "check service; reuse the same request_id",
                }
            )

    return wrapped


@_safe_handler
def dial(args: dict, **kwargs) -> str:
    if set(args) != {"extension", "message", "request_id"}:
        raise ValueError("extension, message and request_id are required")
    if not isinstance(args["request_id"], str) or not re.fullmatch(
        r"[a-f0-9]{32}",
        args["request_id"],
    ):
        raise ValueError("request_id must be 32 lowercase hex characters")
    return _request("POST", "/calls", args)


@_safe_handler
def status(args: dict, **kwargs) -> str:
    if args:
        raise ValueError("status takes no arguments")
    return _request("GET", "/calls")


def register(ctx):
    schemas = [
        (
            "hermes_call_dial",
            dial,
            {
                "description": (
                    "Call an allowed SIP extension NOW and converse using Hermes. "
                    "Use only when requested by the user or an authorized cron task. "
                    "For a future call use Hermes cron to invoke this tool later; this tool has NO "
                    "scheduling. A dialing result means submitted, not answered or delivered. "
                    "Use a new random request_id for each call and reuse it on retries. "
                    "Never automatically redial an unknown call."
                ),
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "extension": {"type": "string", "pattern": "^[0-9]{1,12}$"},
                        "message": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 2000,
                            "description": "Opening words to speak when the call is answered",
                        },
                        "request_id": {
                            "type": "string",
                            "pattern": "^[a-f0-9]{32}$",
                            "description": "Random 32 lowercase hex characters; reuse on retry",
                        },
                    },
                    "required": ["extension", "message", "request_id"],
                },
            },
        ),
        (
            "hermes_call_status",
            status,
            {
                "description": "Read the last 100 call states; unknown needs PBX inspection.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        ),
    ]
    for name, handler, schema in schemas:
        ctx.register_tool(
            name=name, toolset="hermes_call", schema={"name": name, **schema}, handler=handler
        )
