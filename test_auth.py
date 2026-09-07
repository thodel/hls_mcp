"""HTTP Basic auth in front of the MCP endpoint."""
import base64
import pytest

import auth


def _hdr(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


# ── the check itself ─────────────────────────────────────────────────────────

def test_correct_credentials_pass():
    assert auth.check(_hdr("history", "bot"), "history", "bot") is True


@pytest.mark.parametrize("header", [
    "", None,
    "Basic",                                    # scheme only
    "Bearer " + base64.b64encode(b"history:bot").decode(),
    "Basic " + base64.b64encode(b"history").decode(),      # no colon
    "Basic not-base64!!",
    "Basic " + base64.b64encode(b"history:wrong").decode(),
    "Basic " + base64.b64encode(b"wrong:bot").decode(),
])
def test_everything_else_fails(header):
    assert auth.check(header, "history", "bot") is False


def test_the_scheme_is_case_insensitive_as_the_rfc_says():
    assert auth.check("basic " + base64.b64encode(b"history:bot").decode(),
                      "history", "bot") is True


def test_a_password_containing_a_colon_survives_the_split():
    assert auth.check(_hdr("history", "a:b:c"), "history", "a:b:c") is True


def test_undecodable_bytes_are_rejected_not_raised():
    assert auth.check("Basic " + base64.b64encode(b"\xff\xfe").decode(),
                      "history", "bot") is False


# ── wiring ───────────────────────────────────────────────────────────────────

def test_wrap_leaves_the_app_open_when_no_user_is_configured(monkeypatch, caplog):
    monkeypatch.delenv("HLS_AUTH_USER", raising=False)
    sentinel = object()
    with caplog.at_level("WARNING"):
        assert auth.wrap(sentinel) is sentinel
    assert "OPEN" in caplog.text          # it must say so, not fail quietly


def test_wrap_refuses_a_user_without_a_password(monkeypatch):
    monkeypatch.setenv("HLS_AUTH_USER", "history")
    monkeypatch.setenv("HLS_AUTH_PASS", "")
    with pytest.raises(SystemExit):
        auth.wrap(object())


def test_wrap_installs_the_gate_when_configured(monkeypatch):
    monkeypatch.setenv("HLS_AUTH_USER", "history")
    monkeypatch.setenv("HLS_AUTH_PASS", "bot")
    wrapped = auth.wrap(object())
    assert isinstance(wrapped, auth.BasicAuthMiddleware)
    assert wrapped.user == "history" and wrapped.password == "bot"


# ── the ASGI gate ────────────────────────────────────────────────────────────

def _run(mw, headers):
    """Drive the middleware once; return (status, headers, reached_inner)."""
    import asyncio
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "path": "/mcp/hls/mcp", "headers": headers}
    asyncio.run(mw(scope, receive, send))
    start = next((m for m in sent if m["type"] == "http.response.start"), None)
    return start


class _Inner:
    def __init__(self):
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def test_a_request_without_credentials_gets_401_and_a_challenge():
    inner = _Inner()
    mw = auth.BasicAuthMiddleware(inner, "history", "bot")
    start = _run(mw, [])
    assert start["status"] == 401
    assert any(k == b"www-authenticate" for k, _ in start["headers"])
    assert inner.called is False, "the inner app must never see an unauthorised request"


def test_a_request_with_credentials_reaches_the_app():
    inner = _Inner()
    mw = auth.BasicAuthMiddleware(inner, "history", "bot")
    hdr = _hdr("history", "bot").encode("latin-1")
    start = _run(mw, [(b"authorization", hdr)])
    assert start["status"] == 200 and inner.called is True


def test_non_http_scopes_pass_through_untouched():
    import asyncio
    inner = _Inner()
    mw = auth.BasicAuthMiddleware(inner, "history", "bot")

    async def noop_receive():
        return {"type": "lifespan.startup"}

    async def noop_send(msg):
        pass

    asyncio.run(mw({"type": "lifespan"}, noop_receive, noop_send))
    assert inner.called is True
