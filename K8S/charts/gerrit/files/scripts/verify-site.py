#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Verify a Gerrit site installed by the chart, through kubectl.

Port-forwards the release's HTTP Service and checks that:

- the version endpoint answers under the URL prefix;
- ``All-Projects`` clones over HTTP;
- the site survives a pod delete: a marker project created before the
  delete is still there once the StatefulSet has replaced the pod.

The marker needs an account, so the persistence check relies on the
``DEVELOPMENT_BECOME_ANY_ACCOUNT`` authentication the smoke install
uses: it becomes account 1000000, or on a fresh site creates the first
account with a form POST. ``--skip-restart`` leaves it out.

Standard library only, so it runs with any python3 that has kubectl
and git beside it. Each failed check prints one ``FAIL`` line with its
reason, and the script then exits non-zero.

Run it from the repository root, for example:

    python3 K8S/charts/gerrit/files/scripts/verify-site.py \\
        --namespace gerrit-smoke --fullname gerrit --url-prefix /r
"""

from __future__ import annotations

import argparse
import contextlib
import http.cookiejar
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any

XSSI_PREFIX = ")]}'"
HTTP_TIMEOUT = 30
FIRST_ACCOUNT_ID = 1000000


class CheckError(Exception):
    """A check failed; the message is the one-line reason."""


def parse_gerrit_json(body: bytes) -> Any:
    """Decode a Gerrit REST response, stripping the XSSI guard line."""
    text = body.decode("utf-8")
    if text.startswith(XSSI_PREFIX):
        text = text[len(XSSI_PREFIX) :]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CheckError(f"response is not JSON: {text[:80]!r}") from exc


def normalise_prefix(prefix: str) -> str:
    """Return the URL prefix as the chart renders it: "/r" or ""."""
    stripped = prefix.strip("/")
    return f"/{stripped}" if stripped else ""


def kubectl(namespace: str, *args: str, timeout: float = 120) -> str:
    """Run kubectl in the namespace and return its standard output."""
    cmd = ["kubectl", "--namespace", namespace, *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise CheckError(f"{' '.join(cmd)} timed out") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()
        reason = detail[-1] if detail else f"exit code {proc.returncode}"
        raise CheckError(f"{' '.join(cmd)}: {reason}")
    return proc.stdout


@contextlib.contextmanager
def port_forward(
    namespace: str, service: str, local_port: int, timeout: float
) -> Iterator[str]:
    """Port-forward the Service's HTTP port; yield the local base URL."""
    proc = subprocess.Popen(
        [
            "kubectl",
            "--namespace",
            namespace,
            "port-forward",
            f"svc/{service}",
            f"{local_port}:8080",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + timeout
        while True:
            if proc.poll() is not None:
                err = (proc.stderr.read() if proc.stderr else "").strip()
                last = err.splitlines()[-1] if err else "no output"
                raise CheckError(f"kubectl port-forward exited: {last}")
            with contextlib.suppress(OSError):
                socket.create_connection(("127.0.0.1", local_port), 1).close()
                break
            if time.monotonic() > deadline:
                raise CheckError(f"port {local_port} not listening in {timeout}s")
            time.sleep(0.5)
        yield f"http://127.0.0.1:{local_port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if proc.stderr:
            proc.stderr.close()


def http_get(opener: urllib.request.OpenerDirector, url: str) -> bytes:
    """GET a URL, turning any failure into a CheckError."""
    try:
        with opener.open(url, timeout=HTTP_TIMEOUT) as resp:
            body: bytes = resp.read()
            return body
    except urllib.error.HTTPError as exc:
        raise CheckError(f"GET {url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise CheckError(f"GET {url}: {exc}") from exc


def check_version(base: str) -> str:
    """Return the server version the version endpoint reports."""
    body = http_get(urllib.request.build_opener(), f"{base}/config/server/version")
    version = parse_gerrit_json(body)
    if not isinstance(version, str) or not version:
        raise CheckError(f"unexpected version payload: {version!r}")
    return version


def check_clone(base: str, project: str) -> None:
    """Clone a project anonymously over HTTP into a scratch directory."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    with tempfile.TemporaryDirectory(prefix="verify-site-") as tmp:
        cmd = ["git", "clone", "--quiet", f"{base}/{project}", f"{tmp}/clone"]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=env,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CheckError(f"git clone {project} timed out") from exc
    if proc.returncode != 0:
        lines = [ln for ln in proc.stderr.splitlines() if ln.startswith("fatal")]
        reason = lines[-1] if lines else f"exit code {proc.returncode}"
        raise CheckError(f"git clone {project}: {reason}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop at a redirect, so the login reply's own cookies are what count."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        """Decline every redirect; the opener then raises HTTPError."""
        return None


def _login_status(
    opener: urllib.request.OpenerDirector, req: str | urllib.request.Request
) -> int:
    """Send a login request without following redirects; return the status."""
    url = req.full_url if isinstance(req, urllib.request.Request) else req
    try:
        with opener.open(req, timeout=HTTP_TIMEOUT) as resp:
            status: int = resp.status
            return status
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code
    except (urllib.error.URLError, OSError) as exc:
        raise CheckError(f"login {url}: {exc}") from exc


def start_session(base: str) -> tuple[urllib.request.OpenerDirector, str, int]:
    """Log in as the site's first account under DEVELOPMENT_BECOME_ANY_ACCOUNT.

    The first account on a site is an administrator, so it may create
    projects. Become it if an earlier run created it, else create it.
    Returns a cookie-carrying opener, the XSRF token and the account id.
    """
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    login = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar), _NoRedirect()
    )

    def has_session() -> bool:
        return any(c.name == "GerritAccount" for c in jar)

    # A fresh site's first-time redirect filter intercepts every request,
    # /login/ included, until the base page has been fetched once.
    with contextlib.suppress(CheckError):
        http_get(opener, f"{base}/")
    status = _login_status(login, f"{base}/login/?account_id={FIRST_ACCOUNT_ID}")
    if not has_session():
        # The servlet only creates an account on a form POST
        create = urllib.request.Request(
            f"{base}/login/",
            data=b"action=create_account",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        status = _login_status(login, create)
    if not has_session():
        raise CheckError(
            f"development login set no session (HTTP {status}); "
            "is gerrit.auth DEVELOPMENT_BECOME_ANY_ACCOUNT?"
        )
    # The login reply sets no XSRF_TOKEN; the base page does, once signed in
    with contextlib.suppress(CheckError):
        http_get(opener, f"{base}/")
    xsrf = next((c.value for c in jar if c.name == "XSRF_TOKEN"), None)
    me = parse_gerrit_json(http_get(opener, f"{base}/a/accounts/self"))
    account_id = me.get("_account_id") if isinstance(me, dict) else None
    if not isinstance(account_id, int):
        raise CheckError(f"accounts/self after login: unexpected reply {me!r}")
    if not xsrf:
        raise CheckError(f"no XSRF_TOKEN cookie after login as {account_id}")
    return opener, xsrf, account_id


def create_marker(base: str) -> str:
    """Create a uniquely named project as a development account."""
    opener, xsrf, _ = start_session(base)
    name = f"verify-site-{secrets.token_hex(6)}"
    req = urllib.request.Request(
        f"{base}/projects/{name}",
        data=b'{"description": "verify-site.py persistence marker"}',
        method="PUT",
        headers={"Content-Type": "application/json", "X-Gerrit-Auth": xsrf},
    )
    try:
        with opener.open(req, timeout=HTTP_TIMEOUT) as resp:
            created = parse_gerrit_json(resp.read())
    except urllib.error.HTTPError as exc:
        raise CheckError(f"create project {name}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise CheckError(f"create project {name}: {exc}") from exc
    if not isinstance(created, dict) or created.get("id") != name:
        raise CheckError(f"create project {name}: unexpected reply {created!r}")
    return name


def check_marker(base: str, name: str) -> None:
    """Fail unless the marker project still exists."""
    info = parse_gerrit_json(
        http_get(urllib.request.build_opener(), f"{base}/projects/{name}")
    )
    if not isinstance(info, dict) or info.get("id") != name:
        raise CheckError(f"project {name} missing: {info!r}")


def restart_pod(namespace: str, pod: str, timeout: float) -> None:
    """Delete the pod and wait for its replacement to become Ready."""
    old_uid = kubectl(namespace, "get", "pod", pod, "-o", "jsonpath={.metadata.uid}")
    kubectl(namespace, "delete", "pod", pod, "--wait=true", timeout=timeout)
    deadline = time.monotonic() + timeout
    while True:
        try:
            uid = kubectl(
                namespace, "get", "pod", pod, "-o", "jsonpath={.metadata.uid}"
            )
        except CheckError:
            uid = ""
        if uid and uid != old_uid:
            break
        if time.monotonic() > deadline:
            raise CheckError(f"pod {pod} not recreated in {timeout}s")
        time.sleep(2)
    remaining = max(1, int(deadline - time.monotonic()))
    kubectl(
        namespace,
        "wait",
        "--for=condition=Ready",
        f"pod/{pod}",
        f"--timeout={remaining}s",
        timeout=remaining + 30,
    )


def run_check(failures: list[str], name: str, func: Callable[[], str | None]) -> bool:
    """Run one check, record a one-line failure, and report the result."""
    try:
        detail = func()
    except CheckError as exc:
        failures.append(f"FAIL {name}: {exc}")
        print(failures[-1], flush=True)
        return False
    suffix = f" ({detail})" if detail else ""
    print(f"ok   {name}{suffix}", flush=True)
    return True


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        description="Verify a Gerrit site installed by the chart."
    )
    parser.add_argument("--namespace", required=True)
    parser.add_argument(
        "--fullname",
        default="gerrit",
        help="the chart's fullname: StatefulSet <fullname>, "
        "Service <fullname>-http (default: gerrit)",
    )
    parser.add_argument("--url-prefix", default="/r")
    parser.add_argument("--local-port", type=int, default=18080)
    parser.add_argument(
        "--timeout",
        type=float,
        default=900,
        help="seconds to wait for the replacement pod (default: 900)",
    )
    parser.add_argument(
        "--skip-restart",
        action="store_true",
        help="leave out the pod delete and persistence check",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run every check and return the process exit code."""
    args = parse_args(argv)
    prefix = normalise_prefix(args.url_prefix)
    service = f"{args.fullname}-http"
    pod = f"{args.fullname}-0"
    failures: list[str] = []
    marker = ""

    def forwarded(check: Callable[[str], str | None]) -> Callable[[], str | None]:
        def run() -> str | None:
            with port_forward(args.namespace, service, args.local_port, 60) as url:
                return check(url + prefix)

        return run

    def before_restart(base: str) -> str:
        nonlocal marker
        marker = create_marker(base)
        return marker

    run_check(failures, "version endpoint", forwarded(check_version))
    run_check(
        failures,
        "clone All-Projects over HTTP",
        forwarded(lambda base: check_clone(base, "All-Projects")),
    )
    # Each persistence step needs the one before it to have passed
    if (
        not args.skip_restart
        and run_check(failures, "create persistence marker", forwarded(before_restart))
        and run_check(
            failures,
            f"delete pod {pod} and wait for Ready",
            lambda: restart_pod(args.namespace, pod, args.timeout),
        )
    ):
        run_check(
            failures,
            "marker project survives the restart",
            forwarded(lambda base: check_marker(base, marker)),
        )
        run_check(
            failures,
            "version endpoint after restart",
            forwarded(check_version),
        )

    if failures:
        print(f"{len(failures)} check(s) failed", file=sys.stderr)
        return 1
    print("All site checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
