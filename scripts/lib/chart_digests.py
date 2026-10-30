# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Check that rendered Kubernetes manifests pin every image to a digest.

The chart-smoke CI job renders the Gerrit chart with ``image.digest``
set and runs this module over the ``helm template`` output. It parses
the output as YAML documents rather than matching lines, because YAML
spells the same container many ways: block or flow mappings, quoted
keys, block scalars.

Every mapping that carries a ``containers``, ``initContainers`` or
``ephemeralContainers`` list counts as a pod spec, wherever it sits in
the document. That covers bare Pods and the pod templates of
Deployments, StatefulSets, DaemonSets, Jobs and CronJobs without a
list of kinds to keep current. Each container in those lists must
carry an ``image`` ending in ``@<digest>``, where the digest is the one
the render was given.

Usage::

    python3 scripts/lib/chart_digests.py --digest sha256:<64 hex> FILE...

Exits 0 when every file holds at least one container and every
container image carries the digest, 1 when any does not, and 2 on a
usage error or a file that cannot be read or parsed.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

CONTAINER_KEYS = ("containers", "initContainers", "ephemeralContainers")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
IMAGE_DIGEST_RE = re.compile(r"@(sha256:[0-9a-f]{64})$")


def _walk(node: Any, path: str) -> Iterator[tuple[str, Any]]:
    """Yield ``(path, container)`` for every container under ``node``."""
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}"
            if key in CONTAINER_KEYS and isinstance(value, list):
                for index, container in enumerate(value):
                    yield f"{child}[{index}]", container
            else:
                yield from _walk(value, child)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _walk(item, f"{path}[{index}]")


def _document_name(document: Any, index: int) -> str:
    """Name a document as ``Kind/name``, falling back to its index."""
    if isinstance(document, dict):
        kind = document.get("kind")
        metadata = document.get("metadata")
        name = metadata.get("name") if isinstance(metadata, dict) else None
        if kind and name:
            return f"{kind}/{name}"
    return f"document {index}"


def find_images(text: str) -> list[tuple[str, Any]]:
    """Return ``(location, image)`` for every container in ``text``.

    ``text`` is a multi-document YAML stream such as ``helm template``
    prints. A container that is not a mapping, or has no ``image``,
    yields ``None`` as its image so the caller reports it.
    """
    found: list[tuple[str, Any]] = []
    for index, document in enumerate(yaml.safe_load_all(text)):
        if document is None:
            continue
        name = _document_name(document, index)
        for path, container in _walk(document, ""):
            image = container.get("image") if isinstance(container, dict) else None
            found.append((f"{name}{path}", image))
    return found


def check_digests(text: str, digest: str) -> list[str]:
    """Return one problem per container image not pinned to ``digest``.

    An empty list means ``text`` holds at least one container and every
    container image ends in ``@<digest>``.
    """
    images = find_images(text)
    if not images:
        return ["no containers found"]
    problems = []
    for location, image in images:
        if not isinstance(image, str):
            problems.append(f"{location}: no image")
            continue
        match = IMAGE_DIGEST_RE.search(image)
        if match is None or match.group(1) != digest:
            problems.append(f"{location}: {image} is not pinned to @{digest}")
    return problems


def main(argv: list[str] | None = None) -> int:
    """Check each file named on the command line; see the module doc."""
    parser = argparse.ArgumentParser(
        description="Check that every container image carries a digest."
    )
    parser.add_argument("--digest", required=True, help="sha256:<64 hex>")
    parser.add_argument("files", nargs="+", type=Path, help="rendered manifests")
    args = parser.parse_args(argv)
    if not DIGEST_RE.fullmatch(args.digest):
        print(f"invalid digest: {args.digest}", file=sys.stderr)
        return 2

    failed = False
    for path in args.files:
        try:
            problems = check_digests(path.read_text(encoding="utf-8"), args.digest)
        except (OSError, yaml.YAMLError) as exc:
            print(f"{path}: {exc}", file=sys.stderr)
            return 2
        for problem in problems:
            print(f"{path}: {problem}")
        failed = failed or bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
