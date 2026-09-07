"""HTTP Basic authentication for the MCP endpoint.

The server sits on a public path behind nginx and, on tei, also listens on the
host's port directly. Neither the MCP protocol nor the streamable-HTTP transport
carries a notion of identity, so the gate has to be in front of the ASGI app.

Credentials come from the environment and never from the repository. Auth is ON
whenever HLS_AUTH_USER is set and OFF otherwise, and the server logs which of the
two it is at startup — a deployment that silently serves an open endpoint because
a variable was misspelled is the failure this is meant to prevent.
"""
import base64
import binascii
import logging
import os
import secrets

logger = logging.getLogger(__name__)

REALM = "HLS MCP"


def credentials_from_env():
    """(user, password) if configured, else None."""
    user = os.environ.get("HLS_AUTH_USER", "").strip()
    password = os.environ.get("HLS_AUTH_PASS", "")
    return (user, password) if user else None


def check(header_value, user, password) -> bool:
    """True when the Authorization header carries exactly these credentials.

    Both halves are compared with compare_digest: a plain == leaks the length of
    the shared prefix through timing, which is the whole attack against a short
    password.
    """
    if not header_value:
        return False
    scheme, _, encoded = header_value.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    got_user, sep, got_pass = decoded.partition(":")
    if not sep:
        return False
    return (secrets.compare_digest(got_user, user)
            and secrets.compare_digest(got_pass, password))


class BasicAuthMiddleware:
    """Pure-ASGI gate. Wraps the Starlette app the MCP server builds.

    Pure ASGI rather than BaseHTTPMiddleware on purpose: the streamable-HTTP
    transport keeps long-lived response streams open, and BaseHTTPMiddleware
    buffers through an anyio stream that does not survive that pattern well.
    """

    def __init__(self, app, user: str, password: str, exempt=()):
        self.app = app
        self.user = user
        self.password = password
        self.exempt = tuple(exempt)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path", "") in self.exempt:
            return await self.app(scope, receive, send)
        header = ""
        for k, v in scope.get("headers") or ():
            if k == b"authorization":
                header = v.decode("latin-1")
                break
        if check(header, self.user, self.password):
            return await self.app(scope, receive, send)
        body = b"unauthorised"
        await send({"type": "http.response.start", "status": 401, "headers": [
            (b"www-authenticate", f'Basic realm="{REALM}", charset="UTF-8"'.encode()),
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(body)).encode()),
        ]})
        await send({"type": "http.response.body", "body": body})


def wrap(app, *, exempt=()):
    """Apply the gate if credentials are configured; say plainly which it is."""
    creds = credentials_from_env()
    if creds is None:
        logger.warning("HLS_AUTH_USER is not set — the MCP endpoint is OPEN")
        return app
    user, password = creds
    if not password:
        raise SystemExit("HLS_AUTH_USER is set but HLS_AUTH_PASS is empty — refusing "
                         "to start with a password-less account")
    logger.info(f"HTTP Basic auth is ON for user {user!r}")
    return BasicAuthMiddleware(app, user, password, exempt=exempt)
