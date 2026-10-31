# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for verify-site.py's login and server info checks, against a stub Gerrit."""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).parent.parent
    / "K8S"
    / "charts"
    / "gerrit"
    / "files"
    / "scripts"
    / "verify-site.py"
)
_spec = importlib.util.spec_from_file_location("verify_site", SCRIPT)
assert _spec is not None and _spec.loader is not None
verify_site = importlib.util.module_from_spec(_spec)
sys.modules["verify_site"] = verify_site
_spec.loader.exec_module(verify_site)

XSRF = "xsrf-token"


@dataclass
class StubGerrit:
    """What the stub serves, and the requests it saw."""

    prefix: str
    accounts: set[int] = field(default_factory=set)
    dev_auth: bool = True
    base_visited: bool = False
    server_info: dict[str, object] = field(
        default_factory=lambda: {
            "auth": {"auth_type": "DEVELOPMENT_BECOME_ANY_ACCOUNT"},
            "download": {"schemes": {"http": {}, "ssh": {}}, "archives": []},
        }
    )
    requests: list[tuple[str, str, str, str]] = field(default_factory=list)


def _handler(stub: StubGerrit) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def _session(self) -> int | None:
            for part in self.headers.get("Cookie", "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == "GerritAccount" and value.startswith("sess-"):
                    return int(value.removeprefix("sess-"))
            return None

        def _reply(
            self, code: int, body: bytes = b"", headers: dict[str, str] | None = None
        ) -> None:
            self.send_response(code)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _sign_in(self, account: int) -> None:
            self._reply(
                302,
                headers={
                    "Location": f"{stub.prefix}/",
                    "Set-Cookie": (
                        f"GerritAccount=sess-{account}; Path={stub.prefix}/; HttpOnly"
                    ),
                },
            )

        def _route(self, method: str) -> None:
            url = urllib.parse.urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode() if length else ""
            ctype = self.headers.get("Content-Type", "")
            stub.requests.append((method, self.path, ctype, body))
            p = stub.prefix
            if method == "GET" and url.path == f"{p}/":
                stub.base_visited = True
                headers = {}
                if self._session() is not None:
                    headers["Set-Cookie"] = f"XSRF_TOKEN={XSRF}; Path={p}/"
                self._reply(200, b"<html></html>", headers)
            elif url.path == f"{p}/login/":
                if not stub.base_visited:
                    # Gerrit's first-time redirect, ahead of the login servlet
                    self._reply(302, headers={"Location": f"{p}/intro.html"})
                    return
                if not stub.dev_auth:
                    self._reply(404)
                    return
                query = urllib.parse.parse_qs(url.query)
                form = urllib.parse.parse_qs(body)
                if method == "GET" and query.get("account_id"):
                    account = int(query["account_id"][0])
                    if account in stub.accounts:
                        self._sign_in(account)
                    else:
                        self._reply(404)
                elif (
                    method == "POST"
                    and ctype == "application/x-www-form-urlencoded"
                    and form.get("action") == ["create_account"]
                ):
                    account = 1000000 + len(stub.accounts)
                    stub.accounts.add(account)
                    self._sign_in(account)
                else:
                    # A GET with action=create_account only shows the form
                    self._reply(200, b"<html>login</html>")
            elif method == "GET" and url.path == f"{p}/config/server/info":
                payload = json.dumps(stub.server_info).encode()
                self._reply(200, b")]}'\n" + payload)
            elif method == "GET" and url.path == f"{p}/a/accounts/self":
                session = self._session()
                if session is None:
                    self._reply(401)
                else:
                    payload = json.dumps({"_account_id": session}).encode()
                    self._reply(200, b")]}'\n" + payload)
            else:
                self._reply(404)

        def do_GET(self) -> None:
            self._route("GET")

        def do_POST(self) -> None:
            self._route("POST")

    return Handler


@pytest.fixture(params=["/r", ""], ids=["prefix", "root"])
def gerrit(request: pytest.FixtureRequest) -> Iterator[tuple[StubGerrit, str]]:
    """Serve a stub Gerrit; yield it with the base URL under its prefix."""
    stub = StubGerrit(prefix=request.param)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(stub))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield stub, f"http://127.0.0.1:{server.server_port}{stub.prefix}"
    finally:
        server.shutdown()
        server.server_close()


def _posts(stub: StubGerrit) -> list[tuple[str, str, str, str]]:
    return [r for r in stub.requests if r[0] == "POST"]


def test_becomes_existing_account(gerrit: tuple[StubGerrit, str]) -> None:
    stub, base = gerrit
    stub.accounts.add(1000000)
    _, xsrf, account = verify_site.start_session(base)
    assert (xsrf, account) == (XSRF, 1000000)
    assert _posts(stub) == []
    assert all(path.startswith(f"{stub.prefix}/") for _, path, _, _ in stub.requests)


def test_creates_first_account_with_form_post(
    gerrit: tuple[StubGerrit, str],
) -> None:
    stub, base = gerrit
    _, xsrf, account = verify_site.start_session(base)
    assert (xsrf, account) == (XSRF, 1000000)
    assert _posts(stub) == [
        (
            "POST",
            f"{stub.prefix}/login/",
            "application/x-www-form-urlencoded",
            "action=create_account",
        )
    ]
    assert stub.accounts == {1000000}


def test_session_reaches_marker_requests(gerrit: tuple[StubGerrit, str]) -> None:
    _, base = gerrit
    opener, _, _ = verify_site.start_session(base)
    with opener.open(f"{base}/a/accounts/self") as resp:
        assert verify_site.parse_gerrit_json(resp.read()) == {"_account_id": 1000000}


def test_fails_without_development_login(gerrit: tuple[StubGerrit, str]) -> None:
    stub, base = gerrit
    stub.dev_auth = False
    with pytest.raises(verify_site.CheckError, match="set no session \\(HTTP 404\\)"):
        verify_site.start_session(base)


def test_server_info_matches(gerrit: tuple[StubGerrit, str]) -> None:
    _, base = gerrit
    detail = verify_site.check_server_info(
        base, "DEVELOPMENT_BECOME_ANY_ACCOUNT", ["ssh", "http"]
    )
    assert detail == "auth DEVELOPMENT_BECOME_ANY_ACCOUNT; download schemes http, ssh"


def test_server_info_reports_without_expectations(
    gerrit: tuple[StubGerrit, str],
) -> None:
    stub, base = gerrit
    stub.server_info = {"auth": {"auth_type": "HTTP"}}
    detail = verify_site.check_server_info(base, None, [])
    assert detail == "auth HTTP; download schemes none"


def test_server_info_wrong_auth_type(gerrit: tuple[StubGerrit, str]) -> None:
    _, base = gerrit
    with pytest.raises(
        verify_site.CheckError,
        match="auth type 'DEVELOPMENT_BECOME_ANY_ACCOUNT', expected 'HTTP'",
    ):
        verify_site.check_server_info(base, "HTTP", [])


def test_server_info_missing_download_scheme(
    gerrit: tuple[StubGerrit, str],
) -> None:
    _, base = gerrit
    with pytest.raises(
        verify_site.CheckError,
        match=r"download schemes \['http', 'ssh'\] lack \['anonymous http'\]",
    ):
        verify_site.check_server_info(base, None, ["ssh", "anonymous http"])


def test_parse_args_server_info_options() -> None:
    args = verify_site.parse_args(
        [
            "--namespace",
            "ns",
            "--auth-type",
            "HTTP",
            "--download-scheme",
            "ssh",
            "--download-scheme",
            "http",
        ]
    )
    assert (args.auth_type, args.download_scheme) == ("HTTP", ["ssh", "http"])
    defaults = verify_site.parse_args(["--namespace", "ns"])
    assert (defaults.auth_type, defaults.download_scheme) == (None, [])
