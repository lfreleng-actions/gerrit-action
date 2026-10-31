# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for how the Gerrit chart exposes HTTP and SSH.

Renders ``templates/ingress.yaml`` for each values file under
``K8S/charts/gerrit/examples/``, once as a plain Ingress and once as a
Traefik IngressRoute, and compares it with
``tests/chart/golden/<example>/ingress-<mode>.golden``. Also checks the
SSH Service, the NetworkPolicy rules and the gerrit.config keys the
chart derives from the same values.

After an intended change to the rendered output, regenerate the
goldens with ``UPDATE_GOLDEN=1 uv run pytest tests/chart`` and review
the diff.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

import pytest
import yaml
from chart.test_render import (
    CHART,
    EXAMPLE_NAMES,
    EXAMPLES,
    GOLDEN,
    GOLDEN_HEADER,
    helm,
    read_golden,
)

MODES = {
    "plain": (
        "--set",
        "ingress.enabled=true",
        "--set",
        "ingress.className=nginx",
        "--set",
        "ingress.traefik.enabled=false",
    ),
    "traefik": (
        "--set",
        "ingress.enabled=true",
        "--set",
        "ingress.className=traefik",
        "--set",
        "ingress.traefik.enabled=true",
    ),
}

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm")


def render(*args: str) -> list[dict[str, Any]]:
    """Return every object the chart renders with the given helm options."""
    out = helm("template", "gerrit", str(CHART), "--namespace", "gerrit-test", *args)
    return [d for d in yaml.safe_load_all(out) if d]


def objects(docs: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    """Return the rendered objects of one kind."""
    return [d for d in docs if d["kind"] == kind]


def named(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    """Return the one rendered object with this kind and name."""
    found = [d for d in objects(docs, kind) if d["metadata"]["name"] == name]
    assert len(found) == 1, [d["metadata"]["name"] for d in objects(docs, kind)]
    return found[0]


def gerrit_config(docs: list[dict[str, Any]]) -> str:
    """Return the rendered gerrit.config."""
    config: str = named(docs, "ConfigMap", "gerrit-config")["data"]["gerrit.config"]
    return config


@pytest.mark.parametrize("mode", sorted(MODES))
@pytest.mark.parametrize("example", EXAMPLE_NAMES)
def test_ingress_matches_golden(example: str, mode: str) -> None:
    out = helm(
        "template",
        "gerrit",
        str(CHART),
        "--namespace",
        "gerrit-test",
        "-f",
        str(EXAMPLES / f"{example}.yaml"),
        *MODES[mode],
        "--show-only",
        "templates/ingress.yaml",
    )
    golden = GOLDEN / example / f"ingress-{mode}.golden"
    if os.environ.get("UPDATE_GOLDEN"):
        golden.write_text(GOLDEN_HEADER + out, encoding="utf-8")
    assert out == read_golden(golden)


def test_no_ingress_by_default() -> None:
    docs = render()
    for kind in ("Ingress", "IngressRoute", "Middleware", "Certificate"):
        assert not objects(docs, kind), kind


@pytest.mark.parametrize(
    ("example", "host", "path"),
    [
        ("values-root-prefix", "review.example.org", "/"),
        ("values-one-segment-prefix", "gerrit.example.org", "/gerrit"),
        ("values-two-segment-prefix", "www.example.org", "/code/review"),
    ],
)
def test_ingress_host_and_path_follow_values(
    example: str, host: str, path: str
) -> None:
    docs = render("-f", str(EXAMPLES / f"{example}.yaml"), *MODES["plain"])
    ingress = named(docs, "Ingress", "gerrit")
    assert ingress["metadata"]["annotations"]["cert-manager.io/cluster-issuer"] == (
        "letsencrypt-prod"
    )
    assert ingress["spec"]["ingressClassName"] == "nginx"
    assert ingress["spec"]["tls"] == [{"hosts": [host], "secretName": "gerrit-tls"}]
    [rule] = ingress["spec"]["rules"]
    assert rule["host"] == host
    paths = rule["http"]["paths"]
    expected = [(path, "Prefix")]
    if path != "/":
        # the Exact "/" path carries ingress-nginx's root redirect
        expected.insert(0, ("/", "Exact"))
    assert [(p["path"], p["pathType"]) for p in paths] == expected
    for entry in paths:
        assert entry["backend"]["service"] == {
            "name": "gerrit-http",
            "port": {"name": "http"},
        }


def test_nginx_has_no_root_path_without_the_redirect() -> None:
    docs = render(
        "-f",
        str(EXAMPLES / "values-one-segment-prefix.yaml"),
        *MODES["plain"],
        "--set",
        "ingress.redirectRootToPrefix=false",
    )
    ingress = named(docs, "Ingress", "gerrit")
    annotations = ingress["metadata"]["annotations"]
    assert "nginx.ingress.kubernetes.io/app-root" not in annotations
    [rule] = ingress["spec"]["rules"]
    assert [p["path"] for p in rule["http"]["paths"]] == ["/gerrit"]


def test_ingress_drops_the_port_from_the_host() -> None:
    docs = render("--set", "gerrit.host=gerrit.example.org:8443", *MODES["plain"])
    [rule] = named(docs, "Ingress", "gerrit")["spec"]["rules"]
    assert rule["host"] == "gerrit.example.org"


def test_traefik_issues_a_certificate() -> None:
    docs = render(*MODES["traefik"])
    assert not objects(docs, "Ingress")
    cert = named(docs, "Certificate", "gerrit-tls")
    assert cert["spec"]["secretName"] == "gerrit-tls"
    assert cert["spec"]["dnsNames"] == ["gerrit.example.org"]
    assert cert["spec"]["issuerRef"] == {
        "group": "cert-manager.io",
        "kind": "ClusterIssuer",
        "name": "letsencrypt-prod",
    }
    route = named(docs, "IngressRoute", "gerrit")
    assert route["spec"]["tls"] == {"secretName": "gerrit-tls"}


def test_traefik_class_annotation_yields_to_the_users() -> None:
    key = "kubernetes.io/ingress.class"
    route = named(render(*MODES["traefik"]), "IngressRoute", "gerrit")
    assert route["metadata"]["annotations"][key] == "traefik"
    docs = render(
        *MODES["traefik"],
        "--set-string",
        "ingress.annotations.kubernetes\\.io/ingress\\.class=traefik-internal",
    )
    route = named(docs, "IngressRoute", "gerrit")
    assert route["metadata"]["annotations"][key] == "traefik-internal"


def test_traefik_security_headers_wrap_the_root_redirect() -> None:
    route = named(render(*MODES["traefik"]), "IngressRoute", "gerrit")
    [root] = [r for r in route["spec"]["routes"] if "Path(`/`)" in r["match"]]
    assert [m["name"] for m in root["middlewares"]] == [
        "gerrit-security-headers",
        "gerrit-root-redirect",
    ]


def test_traefik_default_csp_allows_blob_workers() -> None:
    headers = named(render(*MODES["traefik"]), "Middleware", "gerrit-security-headers")
    policy = headers["spec"]["headers"]["contentSecurityPolicy"]
    assert "worker-src 'self' blob:" in policy.split("; ")


def test_tls_without_issuer_uses_the_operators_secret() -> None:
    args = ("--set", "ingress.tls.issuer=", "--set", "ingress.tls.secretName=own-tls")
    plain = render(*MODES["plain"], *args)
    ingress = named(plain, "Ingress", "gerrit")
    assert "cert-manager.io/cluster-issuer" not in ingress["metadata"]["annotations"]
    assert ingress["spec"]["tls"][0]["secretName"] == "own-tls"
    traefik = render(*MODES["traefik"], *args)
    assert not objects(traefik, "Certificate")


@pytest.mark.parametrize(
    ("tls", "web", "listen"),
    [("true", "https", "proxy-https"), ("false", "http", "proxy-http")],
)
def test_ingress_tls_sets_the_url_schemes(tls: str, web: str, listen: str) -> None:
    docs = render(
        *MODES["plain"],
        "--set",
        f"ingress.tls.enabled={tls}",
        "--set",
        "gerrit.webScheme=ftp",
        "--set",
        "gerrit.listenScheme=ftp",
    )
    config = gerrit_config(docs)
    assert f"canonicalWebUrl = {web}://gerrit.example.org/r/" in config
    assert f"listenUrl = {listen}://*:8080/r/" in config


@pytest.mark.parametrize(
    ("args", "address"),
    [
        ((), "gerrit.example.org:29418"),
        (("--set", "gerrit.host=localhost:8080"), "localhost:29418"),
        (("--set", "ssh.host=ssh.example.org"), "ssh.example.org:29418"),
    ],
)
def test_ssh_advertised_address_follows_ssh_host(
    args: tuple[str, ...], address: str
) -> None:
    config = gerrit_config(render(*args, "--set", "ssh.advertisedAddress=old:1"))
    assert f"advertisedAddress = {address}" in config


@pytest.mark.parametrize(
    ("args", "address", "node_port"),
    [
        (("--set", "ssh.port=2222"), "gerrit.example.org:2222", None),
        (
            (
                "--set",
                "ssh.service.type=NodePort",
                "--set",
                "ssh.service.nodePort=30022",
            ),
            "gerrit.example.org:30022",
            30022,
        ),
        (
            (
                "--set",
                "ssh.service.type=NodePort",
                "--set",
                "ssh.service.nodePort=30022",
                "--set",
                "ssh.port=29418",
            ),
            "gerrit.example.org:29418",
            30022,
        ),
        (("--set", "ssh.service.nodePort=30022"), "gerrit.example.org:29418", None),
    ],
)
def test_ssh_advertised_port_follows_the_service(
    args: tuple[str, ...], address: str, node_port: int | None
) -> None:
    docs = render(*args)
    assert f"advertisedAddress = {address}" in gerrit_config(docs)
    [port] = named(docs, "Service", "gerrit-ssh")["spec"]["ports"]
    assert port["port"] == 29418
    assert port.get("nodePort") == node_port


def test_node_port_ssh_service_needs_a_known_port() -> None:
    proc = subprocess.run(
        [
            "helm",
            "template",
            "gerrit",
            str(CHART),
            "--set",
            "ssh.service.type=NodePort",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode != 0
    assert "ssh.service.nodePort" in proc.stderr


def test_ssh_service_defaults() -> None:
    service = named(render(), "Service", "gerrit-ssh")
    assert service["spec"]["type"] == "LoadBalancer"
    assert service["spec"]["externalTrafficPolicy"] == "Local"
    assert "annotations" not in service["metadata"]


def test_ssh_service_annotations_and_external_dns() -> None:
    docs = render(
        "--set",
        "ssh.host=ssh.example.org",
        "--set",
        "ssh.service.externalDNS=true",
        "--set-string",
        "ssh.service.annotations.service\\.beta\\.kubernetes\\.io/aws-load-balancer-type=external",
    )
    annotations = named(docs, "Service", "gerrit-ssh")["metadata"]["annotations"]
    assert annotations == {
        "service.beta.kubernetes.io/aws-load-balancer-type": "external",
        "external-dns.alpha.kubernetes.io/hostname": "ssh.example.org",
    }


def test_cluster_ip_ssh_service_has_no_traffic_policy() -> None:
    docs = render(
        "--set", "ssh.service.enabled=true", "--set", "ssh.service.type=ClusterIP"
    )
    assert "externalTrafficPolicy" not in named(docs, "Service", "gerrit-ssh")["spec"]


def test_external_dns_refuses_the_http_host() -> None:
    proc = subprocess.run(
        [
            "helm",
            "template",
            "gerrit",
            str(CHART),
            "--set",
            "ingress.enabled=true",
            "--set",
            "ssh.service.externalDNS=true",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode != 0
    assert "ssh.host" in proc.stderr


def ingress_rules(*args: str) -> dict[int, list[Any] | None]:
    """Map each allowed ingress port to the rule's from list."""
    allow = named(render(*args), "NetworkPolicy", "gerrit-allow")
    return {
        port["port"]: rule.get("from")
        for rule in allow["spec"]["ingress"]
        for port in rule["ports"]
    }


def test_network_policy_admits_http_and_ssh_from_anywhere_by_default() -> None:
    assert ingress_rules() == {8080: None, 29418: None}


def test_network_policy_limits_http_to_the_ingress_namespace() -> None:
    rules = ingress_rules(
        "--set",
        "networkPolicy.ingressNamespaceSelector.matchLabels."
        "kubernetes\\.io/metadata\\.name=traefik",
    )
    assert rules == {
        8080: [
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": "traefik"}
                }
            }
        ],
        29418: None,
    }
