# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Write operations against org-level GitHub Actions configuration.

The audit modules only read; this module is the only place that
mutates a GitHub organisation.  It owns the two primitives used by
``g2p_org_setup: provision``:

* :func:`provision_org_secret` — fetch the org public key, seal the
  value with libsodium (PyNaCl), and ``PUT`` the encrypted secret.
* :func:`provision_org_variable` — ``POST`` a new variable or
  ``PATCH`` an existing one, retrying with ``PATCH`` if GitHub
  reports a conflict.

Both require a token with org admin scope; failures are returned as
:class:`~g2p_github_model.G2PCheckResult` records rather than raised,
so the caller can report every provisioning outcome together.
"""

from __future__ import annotations

import json
import logging
from urllib.error import URLError

from g2p_github_model import GITHUB_API_BASE, G2PCheckResult
from g2p_github_transport import github_request

logger = logging.getLogger(__name__)


def provision_org_secret(
    token: str,
    owner: str,
    secret_name: str,
    secret_value: str,
) -> G2PCheckResult:
    """Create or update an org-level Actions secret.

    Fetches the org public key, encrypts the value with PyNaCl,
    and PUTs the encrypted secret.

    Parameters
    ----------
    token:
        GitHub PAT with org admin scope.
    owner:
        GitHub organisation login.
    secret_name:
        Name of the secret to create/update.
    secret_value:
        Plaintext value to encrypt and store.

    Returns
    -------
    G2PCheckResult
        Passed if the secret was created/updated successfully.
    """
    # Step 1: Fetch the org public key
    key_url = f"{GITHUB_API_BASE}/orgs/{owner}/actions/secrets/public-key"
    try:
        status, key_data = github_request(key_url, token)
    except URLError as exc:
        return G2PCheckResult(
            check_name=f"provision_secret_{secret_name}",
            passed=False,
            message=f"Network error fetching org public key: {exc}",
            severity="error",
        )

    if status != 200 or not isinstance(key_data, dict):
        return G2PCheckResult(
            check_name=f"provision_secret_{secret_name}",
            passed=False,
            message=(f"Failed to fetch org public key for '{owner}' (HTTP {status})"),
            severity="error",
        )

    key_id = key_data.get("key_id", "")
    public_key_b64 = key_data.get("key", "")
    if not key_id or not public_key_b64:
        return G2PCheckResult(
            check_name=f"provision_secret_{secret_name}",
            passed=False,
            message="Org public key response missing key_id or key",
            severity="error",
        )

    # Step 2: Encrypt the secret value
    try:
        import base64

        from nacl.public import (  # pyright: ignore[reportMissingImports]
            PublicKey,
            SealedBox,
        )

        public_key_bytes = base64.b64decode(public_key_b64)
        sealed_box = SealedBox(PublicKey(public_key_bytes))
        encrypted = sealed_box.encrypt(
            secret_value.encode("utf-8"),
        )
        encrypted_b64 = base64.b64encode(encrypted).decode("utf-8")
    except ImportError:
        return G2PCheckResult(
            check_name=f"provision_secret_{secret_name}",
            passed=False,
            message=(
                "PyNaCl is required for secret provisioning — "
                "install with: pip install PyNaCl"
            ),
            severity="error",
        )
    except Exception as exc:
        return G2PCheckResult(
            check_name=f"provision_secret_{secret_name}",
            passed=False,
            message=f"Failed to encrypt secret value: {exc}",
            severity="error",
        )

    # Step 3: PUT the encrypted secret
    put_url = f"{GITHUB_API_BASE}/orgs/{owner}/actions/secrets/{secret_name}"
    body = json.dumps(
        {
            "encrypted_value": encrypted_b64,
            "key_id": key_id,
            "visibility": "all",
        }
    ).encode("utf-8")

    try:
        status, _ = github_request(put_url, token, method="PUT", body=body)
    except URLError as exc:
        return G2PCheckResult(
            check_name=f"provision_secret_{secret_name}",
            passed=False,
            message=f"Network error creating secret: {exc}",
            severity="error",
        )

    if status in (201, 204):
        return G2PCheckResult(
            check_name=f"provision_secret_{secret_name}",
            passed=True,
            message=f"Created/updated org secret '{secret_name}'",
            severity="info",
        )

    return G2PCheckResult(
        check_name=f"provision_secret_{secret_name}",
        passed=False,
        message=(f"Failed to create org secret '{secret_name}' (HTTP {status})"),
        severity="error",
        details={"status": status},
    )


def provision_org_variable(
    token: str,
    owner: str,
    variable_name: str,
    variable_value: str,
    *,
    exists: bool = False,
) -> G2PCheckResult:
    """Create or update an org-level Actions variable.

    Uses POST for new variables and PATCH for existing ones.

    Parameters
    ----------
    token:
        GitHub PAT with org admin scope.
    owner:
        GitHub organisation login.
    variable_name:
        Name of the variable.
    variable_value:
        Value to set.
    exists:
        Whether the variable already exists (use PATCH).

    Returns
    -------
    G2PCheckResult
        Passed if the variable was created/updated.
    """
    body_dict: dict[str, str] = {
        "name": variable_name,
        "value": variable_value,
        "visibility": "all",
    }
    body = json.dumps(body_dict).encode("utf-8")

    if exists:
        url = f"{GITHUB_API_BASE}/orgs/{owner}/actions/variables/{variable_name}"
        method = "PATCH"
    else:
        url = f"{GITHUB_API_BASE}/orgs/{owner}/actions/variables"
        method = "POST"

    try:
        status, _ = github_request(url, token, method=method, body=body)
    except URLError as exc:
        return G2PCheckResult(
            check_name=f"provision_variable_{variable_name}",
            passed=False,
            message=f"Network error creating variable: {exc}",
            severity="error",
        )

    # POST returns 201, PATCH returns 204 (no content)
    if status in (201, 204):
        action = "Updated" if exists else "Created"
        return G2PCheckResult(
            check_name=f"provision_variable_{variable_name}",
            passed=True,
            message=(f"{action} org variable '{variable_name}'"),
            severity="info",
        )

    # 409 on POST means it already exists — retry with PATCH
    if status == 409 and not exists:
        logger.info(
            "Variable '%s' already exists; switching to PATCH",
            variable_name,
        )
        return provision_org_variable(
            token,
            owner,
            variable_name,
            variable_value,
            exists=True,
        )

    return G2PCheckResult(
        check_name=f"provision_variable_{variable_name}",
        passed=False,
        message=(
            f"Failed to create/update org variable '{variable_name}' (HTTP {status})"
        ),
        severity="error",
        details={"status": status},
    )
