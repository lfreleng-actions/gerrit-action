# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Login strategies for Gerrit's DEVELOPMENT_BECOME_ANY_ACCOUNT mode.

Owns *how* an authenticated session is obtained, layered on top of the
session-state handling in :mod:`gerrit_api_session`:

* :meth:`GerritAuthClient.become_account` — log in as a specific,
  already-existing account via ``/login/?account_id=<id>``.
* :meth:`GerritAuthClient._create_first_account` — bootstrap the very
  first account on a fresh instance via ``?action=create_account``.
* :meth:`GerritAuthClient.become_admin` — the escalating strategy that
  combines the two, with a post-bootstrap retry.
"""

from __future__ import annotations

import logging
import time

from gerrit_api_protocol import (
    DEFAULT_ADMIN_ACCOUNTS,
    GerritAPIError,
    GerritAuthError,
)
from gerrit_api_ssh_keys import GerritSshKeyClient

logger = logging.getLogger(__name__)


class GerritAuthClient(GerritSshKeyClient):
    """Adds login and bootstrap strategies to the SSH key client."""

    def become_account(self, account_id: int) -> bool:
        """
        Authenticate by "becoming" the specified account.

        In DEVELOPMENT_BECOME_ANY_ACCOUNT mode, Gerrit allows becoming any
        account by visiting /login/?account_id=<id>. This sets session cookies
        including the XSRF token needed for write operations.

        On a fresh Gerrit instance the OOTB ``FirstTimeRedirect`` filter
        can intercept the login request, so we first dismiss it by
        fetching the base URL.  The login request itself is made with
        ``allow_redirects=False`` to capture the ``Set-Cookie`` header
        directly from the 302 response, avoiding redirect chains that
        may strip the context path and land on a 404.  After obtaining
        the session cookie we explicitly fetch the base page to acquire
        the XSRF token (which is set by the PolyGerrit front-end).

        After obtaining the session cookie, this method verifies that the
        cookie would actually be transmitted by preparing (but not sending)
        a request and inspecting the ``Cookie`` header.  Python's
        ``http.cookiejar`` has known issues with ``localhost`` cookies
        (they can be stored in the jar but never sent back), so the
        verification step catches this and applies a domain workaround.
        If the workaround is needed, ``accounts/self`` is called to
        confirm the session is valid end-to-end.

        Args:
            account_id: The account ID to become (e.g., 1000000 for default admin)

        Returns:
            True if authentication succeeded

        Raises:
            GerritAuthError: If authentication fails
        """
        # --- Dismiss OOTB first-time redirect on fresh instances -----------
        # The FirstTimeRedirect filter intercepts *all* requests (including
        # /login/) on the very first access.  Pre-fetching the base URL
        # satisfies the filter so that /login/ is handled normally.
        self._dismiss_ootb_redirect()

        # Login endpoint lives under the same context path as all other endpoints.
        # When Gerrit is configured with httpd.listenUrl that includes a path
        # (e.g., http://*:8080/r/), the login endpoint is at /r/login/, not /login/.
        login_url = f"{self.base_url}/login/?account_id={account_id}"
        logger.debug(f"Becoming account {account_id} via {login_url}")

        # --- Do NOT follow redirects ----------------------------------------
        # The login servlet returns a 302 with Set-Cookie in the *first*
        # response.  Following redirects can land on pages outside the
        # context path (e.g. /plugins/plugin-manager/static/intro.html)
        # which returns 404 and may confuse cookie handling.  We only
        # need the Set-Cookie header from the 302 itself.
        response = self.session.get(
            login_url,
            allow_redirects=False,
            timeout=self.timeout,
            verify=self.verify_ssl,
        )

        # Log detailed response information for debugging authentication
        # failures (e.g. OOTB redirect issues, context-path mismatches).
        logger.debug(
            f"Login response: HTTP {response.status_code}, "
            f"Location: {response.headers.get('Location', '(none)')}"
        )
        cookie_details = [(c.name, c.domain, c.path) for c in self.session.cookies]
        logger.debug(f"Session cookies after login: {cookie_details}")

        # Check for GerritAccount cookie
        has_account_cookie = any(
            c.name == "GerritAccount" for c in self.session.cookies
        )

        if not has_account_cookie:
            raise GerritAuthError(
                f"Failed to become account {account_id}: no session cookie set "
                f"(HTTP {response.status_code}, "
                f"Location: {response.headers.get('Location', '(none)')})",
                status_code=response.status_code,
            )

        # Extract and store XSRF token (may be missing at this point)
        self._xsrf_token = self._extract_xsrf_token()
        self._account_id = account_id

        # --- Ensure XSRF token is available ---------------------------------
        # The XSRF_TOKEN cookie is set by the PolyGerrit front-end page,
        # not by the login servlet itself.  Since we no longer follow the
        # redirect chain, we must explicitly fetch the base page.
        self._ensure_xsrf_token()

        logger.debug(
            f"Successfully became account {account_id}, "
            f"XSRF token: {'present' if self._xsrf_token else 'missing'}"
        )

        # --- Verify the cookie actually works ---
        # Python's http.cookiejar can silently refuse to send cookies for
        # "localhost" due to domain-matching rules.  Detect this by making
        # a test request and checking whether the cookie was transmitted.
        self._verify_auth_or_fix_cookies(account_id)

        return True

    def _create_first_account(self) -> int:
        """Create the first account on a fresh Gerrit instance.

        In ``DEVELOPMENT_BECOME_ANY_ACCOUNT`` mode, the
        ``BecomeAnyAccountLoginServlet`` exposes an
        ``?action=create_account`` endpoint (via POST) that:

        1. Creates a new account with an auto-generated username
           (``user1``, ``user2``, …) and a UUID-based external ID.
        2. Logs the session in as the newly created account (sets the
           ``GerritAccount`` cookie).
        3. Redirects to the Gerrit root.

        This is the **correct** bootstrap mechanism for a fresh instance
        where zero accounts exist.  The older ``?account_id=X`` approach
        requires the account to already be present in the database.

        Returns:
            The ``_account_id`` of the newly created account.

        Raises:
            GerritAuthError: If account creation or authentication fails.
        """
        # Dismiss the OOTB first-time redirect before the bootstrap POST,
        # just as we do in become_account().
        self._dismiss_ootb_redirect()

        login_url = f"{self.base_url}/login/"
        logger.info("Creating first account via login servlet (action=create_account)")
        logger.debug(f"POST {login_url} data=action=create_account")

        try:
            # Use allow_redirects=False to capture the Set-Cookie header
            # from the 302 response directly, avoiding redirect chains
            # that may strip the context path.
            response = self.session.post(
                login_url,
                data={"action": "create_account"},
                allow_redirects=False,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        except Exception as exc:
            raise GerritAuthError(
                f"Bootstrap POST to {login_url} failed: {exc}"
            ) from exc

        logger.debug(
            f"Bootstrap response: HTTP {response.status_code}, "
            f"Location: {response.headers.get('Location', '(none)')}"
        )
        cookie_names = [c.name for c in self.session.cookies]
        logger.debug(f"Session cookies after bootstrap: {cookie_names}")

        # Verify that the servlet set a session cookie
        has_account_cookie = any(
            c.name == "GerritAccount" for c in self.session.cookies
        )
        if not has_account_cookie:
            raise GerritAuthError(
                "Bootstrap account creation did not set a session cookie "
                f"(HTTP {response.status_code}, "
                f"Location: {response.headers.get('Location', '(none)')})",
                status_code=response.status_code,
            )

        # Extract XSRF token and fetch base page if needed
        self._xsrf_token = self._extract_xsrf_token()
        self._ensure_xsrf_token()

        # Discover the account ID of the account we just created
        try:
            account_info = self.get_account("self")
            account_id: int = account_info["_account_id"]
        except (GerritAPIError, KeyError) as exc:
            raise GerritAuthError(
                "Bootstrap account was created but failed to retrieve "
                f"account details: {exc}"
            ) from exc

        self._account_id = account_id
        logger.info(f"Bootstrapped first account (ID: {account_id})")
        return account_id

    def become_admin(self) -> int:
        """Become an admin account, bootstrapping if necessary.

        Authentication strategy (each step is attempted only if the
        previous one failed):

        1. **Try known admin account IDs** – on a previously initialised
           instance accounts 1000000 and 1 typically exist already.
        2. **Bootstrap via ``action=create_account``** – on a *fresh*
           instance no accounts exist.  The ``BecomeAnyAccountLoginServlet``
           can create the first account and authenticate us in one step.
        3. **Retry known IDs** – the bootstrap POST may have created the
           account as a side-effect but failed to set the session cookie
           (e.g. due to a context-path redirect mismatch).  A brief pause
           lets the account be indexed, then we retry ``?account_id=X``.

        Returns:
            The account ID that was successfully authenticated.

        Raises:
            GerritAuthError: If no admin account could be authenticated
                after all strategies have been exhausted.
        """
        errors: list[str] = []

        # --- Pass 1: try known admin account IDs (fast path) ----------------
        for account_id in DEFAULT_ADMIN_ACCOUNTS:
            try:
                self.become_account(account_id)
                logger.info(f"Authenticated as admin account {account_id}")
                return account_id
            except GerritAuthError as exc:
                logger.debug(f"become_account({account_id}) failed: {exc}")
                errors.append(f"become({account_id}): {exc}")

        # --- Pass 2: bootstrap first account via login servlet ---------------
        logger.info(
            "No existing admin account found. "
            "Bootstrapping first account via login servlet..."
        )
        try:
            account_id = self._create_first_account()
            return account_id
        except GerritAuthError as exc:
            logger.warning(f"Bootstrap account creation failed: {exc}")
            errors.append(f"bootstrap: {exc}")

        # --- Pass 3: retry known IDs after bootstrap side-effect -------------
        # The bootstrap POST may have created the account even though the
        # redirect did not produce a session cookie.  Wait briefly for the
        # account to be indexed and try again.
        logger.info("Retrying known admin account IDs after bootstrap attempt...")
        time.sleep(2)
        for account_id in DEFAULT_ADMIN_ACCOUNTS:
            try:
                self.become_account(account_id)
                logger.info(
                    f"Authenticated as admin account {account_id} "
                    "(post-bootstrap retry)"
                )
                return account_id
            except GerritAuthError as exc:
                logger.debug(
                    f"become_account({account_id}) post-bootstrap failed: {exc}"
                )
                errors.append(f"retry({account_id}): {exc}")

        # All strategies exhausted
        error_detail = "; ".join(errors)
        raise GerritAuthError(
            "Failed to authenticate as any admin account. "
            f"Strategies tried: {error_detail}"
        )
