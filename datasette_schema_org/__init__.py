"""schema.org JSON-LD for Datasette, injected via ASGI response rewriting.

Plugins can't emit <script type="application/ld+json"> inline via any
existing template hook (extra_js_urls forces text/javascript, etc.), so
we intercept HTML responses and splice the script tag in before </head>.
This makes the plugin work regardless of template overrides.
"""

import json
import re

from datasette import hookimpl

from .builders import INTERNAL_DATABASES, build_jsonld

__all__ = ["asgi_wrapper"]

_HASH_RE = re.compile(r"^(?P<name>[A-Za-z0-9_]+?)(?:-[a-f0-9]{7,40})?$")
_HEAD_CLOSE = b"</head>"


class _Request:
    """Minimal request-like object exposing the scheme and host fields
    the builders need to construct absolute URLs."""

    def __init__(self, scope):
        self.scheme = scope.get("scheme", "http")
        host = "localhost"
        for name, value in scope.get("headers") or []:
            if name == b"host":
                host = value.decode("latin-1", "replace")
                break
        self.host = host


def _canonical_db_name(slug, datasette):
    """Map 'nlrb-3de46a1' (datasette-hashed-urls) back to 'nlrb', if it
    resolves to a real database. Returns None for unknown slugs."""
    if slug in datasette.databases:
        return slug
    match = _HASH_RE.match(slug)
    if match and match.group("name") in datasette.databases:
        return match.group("name")
    return None


def _resolve_view(path, datasette):
    """Return (view_name, database, table) for paths we mark up, else None."""
    if path.startswith("/-/") or path.startswith("/_internal"):
        return None
    cleaned = path.strip("/")
    if not cleaned:
        return ("index", None, None)
    segments = cleaned.split("/")
    if len(segments) > 2:
        return None
    db_name = _canonical_db_name(segments[0], datasette)
    if db_name is None or db_name in INTERNAL_DATABASES:
        return None
    if len(segments) == 1:
        return ("database", db_name, None)
    return ("table", db_name, segments[1])


def _is_html_response(headers, status):
    if status != 200:
        return False
    for name, value in headers:
        if name.lower() == b"content-type":
            return value.decode("latin-1", "replace").startswith("text/html")
    return False


def _inject(body, jsonld):
    """Insert the JSON-LD script tag immediately before </head>. Returns
    the rewritten body, or the original body if </head> is not present."""
    idx = body.lower().find(_HEAD_CLOSE)
    if idx < 0:
        return body
    script = (
        b'<script type="application/ld+json">'
        + json.dumps(jsonld, indent=2).encode("utf-8")
        + b"</script>\n"
    )
    return body[:idx] + script + body[idx:]


@hookimpl
def asgi_wrapper(datasette):
    def wrap(app):
        async def wrapped(scope, receive, send):
            if scope["type"] != "http":
                await app(scope, receive, send)
                return

            resolved = _resolve_view(scope.get("path", "/"), datasette)
            if resolved is None:
                await app(scope, receive, send)
                return

            view_name, database, table = resolved
            state = {"status": None, "headers": [], "buffering": False, "body": b""}

            async def intercept(message):
                kind = message["type"]

                if kind == "http.response.start":
                    state["status"] = message["status"]
                    state["headers"] = list(message.get("headers") or [])
                    if _is_html_response(state["headers"], state["status"]):
                        state["buffering"] = True
                    else:
                        await send(message)
                    return

                if kind != "http.response.body":
                    await send(message)
                    return

                if not state["buffering"]:
                    await send(message)
                    return

                state["body"] += message.get("body") or b""
                if message.get("more_body"):
                    return

                # Final chunk: build JSON-LD, splice, emit rewritten response.
                jsonld = None
                try:
                    jsonld = await build_jsonld(
                        view_name=view_name,
                        database=database,
                        table=table,
                        request=_Request(scope),
                        datasette=datasette,
                    )
                except Exception:
                    pass

                body = state["body"]
                headers = state["headers"]
                if jsonld:
                    new_body = _inject(body, jsonld)
                    if new_body is not body:
                        headers = [
                            (name, value)
                            for name, value in headers
                            if name.lower() != b"content-length"
                        ]
                        headers.append(
                            (b"content-length", str(len(new_body)).encode("ascii"))
                        )
                        body = new_body

                await send(
                    {
                        "type": "http.response.start",
                        "status": state["status"],
                        "headers": headers,
                    }
                )
                await send({"type": "http.response.body", "body": body})

            await app(scope, receive, intercept)

        return wrapped

    return wrap
