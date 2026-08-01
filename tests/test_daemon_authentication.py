"""The token check on the daemon's own listeners, for both kinds of connection.

These run against the real application rather than against a stub of the middleware. The bug
they exist to prevent was not in the policy — the policy was right — but in which connections
the policy was applied to, and only the assembled app can answer that. A test that wrapped the
middleware around a toy route would have passed against the broken version.

`/terminal` is the case that matters. It is a PTY, or an SSH login shell to a configured host,
so an unauthenticated handshake there is arbitrary command execution for anything that can
reach the port.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from frank.daemon import state
from frank.daemon.__main__ import build_app

TOKEN = "a-daemon-token-that-is-not-guessed"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The daemon's app, with a known token and no registry.

    No registry means the session-token fallback finds nobody, which is the state a fresh daemon
    is in and the one in which a missing check is most exposed.
    """
    monkeypatch.setattr(state, "daemon_token", TOKEN, raising=False)
    monkeypatch.setattr(state, "registry", None, raising=False)
    return TestClient(build_app())


# ── Websockets ────────────────────────────────────────────────────────────────────────────────

def test_a_terminal_handshake_without_a_token_is_refused(client: TestClient) -> None:
    """The hole this file was written for.

    The middleware began `if scope["type"] != "http": return await self.application(...)`, so
    every handshake went straight through. The token was already being sent — `frank serve`'s
    proxy appends it, and so does the web client — and nothing compared it to anything.
    """
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/terminal") as socket:
            socket.receive_text()


def test_a_terminal_handshake_with_the_wrong_token_is_refused(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/terminal?token=nearly-right") as socket:
            socket.receive_text()


def test_a_terminal_handshake_with_the_token_is_let_through(client: TestClient) -> None:
    """Past the guard, not necessarily to a shell.

    There is no terminal manager on a daemon that was never started, so the route answers with
    `terminal_unavailable` — which is the proof wanted here. A refused handshake never reaches a
    route at all, so any reply from the route is the check having passed.
    """
    with client.websocket_connect(f"/terminal?token={TOKEN}") as socket:
        assert socket.receive_json()["code"] == "terminal_unavailable"


def test_the_refusal_happens_before_the_socket_is_accepted(client: TestClient) -> None:
    """Refusing means closing without accepting, which ASGI turns into a failed handshake.

    Accepting and then closing would read to a client as a connection that was allowed and then
    dropped, and would give the route a moment in which it believed it had a client.
    """
    with pytest.raises(WebSocketDisconnect) as refusal:
        with client.websocket_connect("/terminal"):
            pass
    # 1008 is "policy violation", the closest the protocol has to a 401.
    assert refusal.value.code == 1008


# ── HTTP, unchanged ───────────────────────────────────────────────────────────────────────────

def test_an_http_request_without_a_token_is_still_refused(client: TestClient) -> None:
    response = client.post("/rpc", json={"method": "daemon.status", "params": {}})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_the_token_is_accepted_in_a_header_or_a_query_parameter(client: TestClient) -> None:
    """A method the daemon does not have, so that what comes back is about the *method*.

    Asking for a real one would answer about whatever daemon state a bare app has not got, and
    the question here is only whether the call was let through at all — `no_such_method` says it
    was, and `unauthorized` says it was not.
    """
    call = {"method": "no.such.method", "params": {}}
    with_header = client.post("/rpc", json=call, headers={"Authorization": f"Bearer {TOKEN}"})
    with_query = client.post(f"/rpc?token={TOKEN}", json=call)
    assert with_header.json()["error"]["code"] == "no_such_method"
    assert with_query.json()["error"]["code"] == "no_such_method"


def test_health_stays_unauthenticated(client: TestClient) -> None:
    """A client has to know the daemon is up before it has read the token, and this says nothing
    else."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "service": "frankd"}


def test_a_cors_preflight_passes_without_a_token(client: TestClient) -> None:
    """A browser sends preflight without credentials by specification. Refusing it rejects the
    question rather than the request, and the real request is then never sent — which is how
    every list in the desktop app once came back empty with no visible error."""
    response = client.options(
        "/rpc",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200


def test_a_bare_options_still_needs_the_token(client: TestClient) -> None:
    """The exemption is for a preflight, not for the method."""
    assert client.options("/rpc").status_code == 401
