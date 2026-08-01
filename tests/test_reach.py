"""What `frank reach` must not get wrong.

This is the one surface in Frank that is meant to leave the machine, so the assertions here are
about the two things that makes true: that nothing without the token gets through, and that the
token itself does not leak onward. Everything else about the command — which addresses it finds,
what the QR code looks like — is presentation and is not tested.

The proxy is stubbed rather than pointed at a daemon. What is under test is the guard, and a real
daemon would only add a second thing that could fail.
"""

from __future__ import annotations

import json

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient

from frank.cli.commands.reach import endpoints, pairing_uri, require_token

TOKEN = "a-token-that-is-not-guessed"


@pytest.fixture
def client() -> TestClient:
    """The guard, wrapped around an application that reports what reached it."""

    async def echo(request) -> JSONResponse:
        return JSONResponse({
            "query": str(request.url.query),
            "cookie": request.headers.get("cookie", ""),
            "seen": True,
        })

    async def socket(websocket) -> None:
        await websocket.accept()
        await websocket.send_text(str(websocket.url.query))
        await websocket.close()

    inner = Starlette(routes=[
        WebSocketRoute("/terminal", socket),
        Route("/{path:path}", echo, methods=["GET", "POST", "OPTIONS"]),
    ])
    return TestClient(require_token(inner, TOKEN))


def test_a_request_without_a_token_is_refused(client: TestClient) -> None:
    response = client.get("/sessions")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_a_request_with_the_wrong_token_is_refused(client: TestClient) -> None:
    assert client.get("/sessions", headers={"Authorization": "Bearer nearly"}).status_code == 401
    assert client.get("/sessions?token=nearly").status_code == 401


def test_the_token_is_accepted_in_a_header_or_a_query_parameter(client: TestClient) -> None:
    assert client.get("/sessions", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert client.get(f"/sessions?token={TOKEN}").status_code == 200


def test_the_token_is_stripped_before_the_request_goes_on(client: TestClient) -> None:
    """The reach token is this process's secret and must not ride along to the daemon.

    Forwarding it would put a durable credential in the daemon's logs, and — worse — into the
    daemon's own token check, where a `token` query parameter is consulted and would shadow the
    `Authorization` header the proxy is about to attach. The proxied call would then fail with a
    401 that named neither cause.
    """
    seen = client.get(f"/sessions?limit=5&token={TOKEN}").json()["query"]
    assert "token" not in seen
    assert seen == "limit=5"


def test_a_websocket_handshake_needs_the_token_too(client: TestClient) -> None:
    """The case an HTTP-shaped check forgets.

    `/terminal` is a shell. A guard that inspects only `scope["type"] == "http"` lets every
    websocket through, which is not a smaller hole than the HTTP one — it is the largest.
    """
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/terminal") as socket:
            socket.receive_text()

    with client.websocket_connect(f"/terminal?token={TOKEN}") as socket:
        assert "token" not in socket.receive_text()


def test_a_document_asked_for_with_a_token_is_answered_with_a_cookie(client: TestClient) -> None:
    """What lets this serve the interface at all.

    A page cannot attach a credential to the hundred subresource requests it is about to make, so
    the app opens the interface once with `?token=…` and the session cookie covers everything
    after it — scripts, fonts, event streams and the terminal's websocket alike.
    """
    response = client.get("/", params={"token": TOKEN}, headers={"Sec-Fetch-Dest": "document"})
    cookie = response.headers.get("set-cookie", "")
    assert "frank_reach=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie


def test_the_cookie_is_then_enough_on_its_own(client: TestClient) -> None:
    client.get("/", params={"token": TOKEN}, headers={"Sec-Fetch-Dest": "document"})
    # The client keeps the cookie, so this carries no token of its own.
    assert client.get("/sessions").status_code == 200


def test_the_cookie_does_not_reach_the_daemon(client: TestClient) -> None:
    """The reach token is this process's secret, whichever envelope it arrived in."""
    client.get("/", params={"token": TOKEN}, headers={"Sec-Fetch-Dest": "document"})
    seen = client.get("/sessions", headers={"Cookie": f"frank_reach={TOKEN}; theme=dark"}).json()
    assert "frank_reach" not in seen.get("cookie", "")
    assert seen.get("cookie") == "theme=dark"


def test_a_subresource_with_a_token_is_not_given_a_cookie(client: TestClient) -> None:
    """Only a document starts a session. A script that happens to carry one is just a request."""
    response = client.get("/main.js", params={"token": TOKEN}, headers={"Sec-Fetch-Dest": "script"})
    assert "set-cookie" not in response.headers


def test_a_cors_preflight_passes_without_a_token(client: TestClient) -> None:
    """A browser sends preflight without credentials, by specification.

    Refusing it rejects the question rather than the request, and the real request — the one that
    would have carried the token — is then never sent at all.
    """
    response = client.options(
        "/rpc",
        headers={"Origin": "http://localhost:8081", "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 200


def test_a_bare_options_still_needs_the_token(client: TestClient) -> None:
    """The exemption is for preflight, not for the method. Without the preflight header it is an
    ordinary request and is gated like one."""
    assert client.options("/rpc").status_code == 401


def test_an_advertised_address_wins_and_is_taken_as_given() -> None:
    """Whoever passes `--advertise` knows something this code cannot — that a reverse proxy
    terminates TLS on 443, say — so a value carrying a scheme is not decorated with a port."""
    found = endpoints(8825, secure=False, advertise="https://frank.example.com")
    assert found[0] == "https://frank.example.com"

    found = endpoints(8825, secure=False, advertise="frank.example.com")
    assert found[0] == "http://frank.example.com:8825"


def test_the_pairing_link_keeps_its_payload_in_the_fragment() -> None:
    """A fragment is not sent to a server by anything that resolves a URL, is not written to proxy
    logs, and is not part of what a QR reader shows as the destination — which for a link carrying
    a bearer token is worth the three extra characters."""
    payload = {"version": 1, "name": "somewhere", "token": TOKEN, "endpoints": ["http://10.0.0.2:8825"]}
    uri = pairing_uri(payload)

    scheme, _, rest = uri.partition("://")
    assert scheme == "frank"
    assert "?" not in rest
    assert TOKEN not in rest.split("#")[0]

    import base64

    encoded = rest.split("#", 1)[1]
    decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    assert json.loads(decoded) == payload
