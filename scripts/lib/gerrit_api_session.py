# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Session state management for the Gerrit REST client.

Owns everything to do with keeping an authenticated browser-like session
alive, as distinct from *how* a session is obtained (see
:mod:`gerrit_api_auth`):

* Acquiring and re-extracting the ``XSRF_TOKEN`` cookie that Gerrit's
  PolyGerrit front-end sets and that every write operation needs.
* Dismissing the out-of-the-box ``FirstTimeRedirect`` filter that
  intercepts requests on a brand-new Gerrit instance.
* Verifying that the ``GerritAccount`` session cookie is actually being
  transmitted, and working around the :mod:`http.cookiejar`
  domain-matching bug that silently drops ``localhost`` cookies.
"""

from __future__ import annotations

import contextlib
import http.cookiejar
import logging
from typing import Any
from urllib.parse import urlparse

import requests
from gerrit_api_protocol import GerritAuthError, _cookie_names_from_header
from gerrit_api_transport import GerritTransport

logger = logging.getLogger(__name__)


def _rebind_localhost_cookies(session: requests.Session) -> list[str]:
    """Re-bind every localhost-scoped cookie in *session* to an empty domain.

    Python's :mod:`http.cookiejar` applies domain-matching rules that can
    store a cookie issued by ``localhost`` yet never send it back.  This
    helper removes each localhost-scoped cookie and re-adds an otherwise
    identical cookie whose domain is empty, so that :mod:`requests`
    attaches it unconditionally.

    All original attributes are cloned and only the domain is overridden,
    so flags such as ``secure``, ``expires`` and ``HttpOnly`` survive.

    Args:
        session: The session whose cookie jar should be re-bound.

    Returns:
        The names of the cookies that were re-bound, in jar order.
    """
    fixed: list[str] = []
    originals: list[Any] = []
    for cookie in list(session.cookies):
        domain = getattr(cookie, "domain", "")
        if domain and "localhost" in str(domain):
            originals.append(cookie)
            fixed.append(getattr(cookie, "name", ""))

    # Remove originals, then re-add with empty domain
    for cookie in originals:
        c_name = getattr(cookie, "name", "")
        c_domain = getattr(cookie, "domain", "")
        c_path = getattr(cookie, "path", "/")

        # Defensive: remove any pre-existing empty-domain cookie
        # with the same name to avoid duplicates if run twice.
        with contextlib.suppress(KeyError):
            session.cookies.clear(domain="", path=c_path, name=c_name)
        # Remove the original localhost-scoped cookie.
        # cookiejar may store the domain in various forms, so
        # wrap each clear() individually to tolerate KeyError.
        with contextlib.suppress(KeyError):
            session.cookies.clear(domain=c_domain, path=c_path, name=c_name)
        # Also sweep any remaining cookies with the same name
        # that still carry a localhost domain.
        for remaining in list(session.cookies):
            if getattr(remaining, "name", "") == c_name and "localhost" in str(
                getattr(remaining, "domain", "")
            ):
                with contextlib.suppress(KeyError):
                    session.cookies.clear(
                        domain=getattr(remaining, "domain", ""),
                        path=getattr(remaining, "path", "/"),
                        name=c_name,
                    )

        # Re-add with domain="" while preserving all other attributes
        # from the original cookie (secure, expires, rest/HttpOnly, …).
        new_cookie = http.cookiejar.Cookie(
            version=getattr(cookie, "version", 0),
            name=c_name,
            value=getattr(cookie, "value", "") or "",
            port=getattr(cookie, "port", None),
            port_specified=getattr(cookie, "port_specified", False),
            domain="",
            domain_specified=False,
            domain_initial_dot=False,
            path=c_path,
            path_specified=getattr(cookie, "path_specified", True),
            secure=getattr(cookie, "secure", False),
            expires=getattr(cookie, "expires", None),
            discard=getattr(cookie, "discard", True),
            comment=getattr(cookie, "comment", None),
            comment_url=getattr(cookie, "comment_url", None),
            rest=getattr(cookie, "_rest", {}),
        )
        session.cookies.set_cookie(new_cookie)

    return fixed


class GerritSessionClient(GerritTransport):
    """Adds cookie/XSRF session handling on top of :class:`GerritTransport`."""

    def _extract_xsrf_token(self) -> str | None:
        """Extract XSRF token from session cookies."""
        for cookie in self.session.cookies:
            if cookie.name == "XSRF_TOKEN":
                return str(cookie.value)
        return None

    def _dismiss_ootb_redirect(self) -> None:
        """Pre-access the Gerrit base URL to clear the OOTB first-time redirect.

        On a **fresh** Gerrit instance the ``FirstTimeRedirect`` servlet
        filter intercepts every request (including ``/login/``) and
        redirects it to ``httpd.firstTimeRedirectUrl`` *before* the
        ``BecomeAnyAccountLoginServlet`` has a chance to run.  This
        means the session cookie is never set on the first login
        attempt.

        Fetching the base URL once satisfies the OOTB filter so that
        subsequent requests (in particular ``/login/``) are handled
        normally by the Gerrit servlets.
        """
        base_page_url = self.base_url + "/"
        logger.debug(
            "Pre-accessing %s to dismiss OOTB first-time redirect", base_page_url
        )
        try:
            self.session.get(
                base_page_url,
                allow_redirects=True,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        except Exception as exc:
            # Non-fatal: the main login flow will surface any real errors.
            logger.debug("OOTB pre-access failed (non-fatal): %s", exc)

    def _ensure_xsrf_token(self) -> None:
        """Fetch the Gerrit index page to obtain the XSRF_TOKEN cookie.

        The XSRF token is set by the PolyGerrit front-end page.  When
        the login redirect chain does not end on the main Gerrit page
        (e.g. because it lands on a 404 from the OOTB plugin-manager
        intro page), the token cookie is never set.

        This method explicitly fetches the base URL while the session
        already carries the ``GerritAccount`` cookie, which causes
        Gerrit to emit the ``XSRF_TOKEN`` cookie in the response.
        """
        if self._xsrf_token:
            return  # Already have one

        base_page_url = self.base_url + "/"
        logger.debug(
            "XSRF token missing after login; fetching %s to obtain it",
            base_page_url,
        )
        try:
            self.session.get(
                base_page_url,
                allow_redirects=True,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            self._xsrf_token = self._extract_xsrf_token()
            if self._xsrf_token:
                logger.debug("XSRF token obtained from base page ✅")
            else:
                logger.warning(
                    "XSRF token still missing after fetching base page; "
                    "write operations may fail"
                )
        except Exception as exc:
            logger.warning("Failed to fetch base page for XSRF token: %s", exc)

    def _sends_auth_cookie(self) -> bool:
        """Return True if the session would transmit the ``GerritAccount`` cookie.

        Prepares — but deliberately does **not** send — a request to
        ``accounts/self`` and inspects the resulting ``Cookie`` header,
        so verification costs no network round-trip.
        """
        verify_url = self._make_url("accounts/self")
        req = requests.Request("GET", verify_url, headers=self._get_headers())
        prepared = self.session.prepare_request(req)
        cookie_header = prepared.headers.get("Cookie", "")
        return "GerritAccount" in _cookie_names_from_header(cookie_header)

    def _verify_auth_or_fix_cookies(self, account_id: int) -> None:
        """Verify session cookies are sent and fix localhost cookie issues.

        After ``become_account`` stores the session cookies, this method
        prepares (but does not send) a request to ``accounts/self`` and
        inspects the ``Cookie`` header to confirm that the session cookie
        would actually be transmitted.  If the header looks good, the
        method returns immediately **without** making a network call
        (header-only verification).

        If the cookie exists in the jar but is **not** present in the
        prepared header (a known ``http.cookiejar`` problem with
        ``localhost`` domains), this method removes the original
        localhost-scoped cookies and re-adds them with an empty domain
        so that ``requests`` will attach them unconditionally.  After
        the fix-up, ``accounts/self`` is called to confirm the session
        is valid end-to-end.

        Raises
        ------
        GerritAuthError
            If the cookie cannot be made to work after the fix-up.
        """
        if self._sends_auth_cookie():
            logger.debug("Auth verification: cookie is transmitted ✅")
            return

        # Guard: only apply the localhost workaround when the base URL
        # actually targets a loopback address.  This prevents cookies
        # from being broadened to domain="" when talking to a real host.
        parsed = urlparse(self.base_url)
        hostname = (parsed.hostname or "").lower()
        is_localhost = hostname in ("localhost", "127.0.0.1", "::1")

        if not is_localhost:
            raise GerritAuthError(
                f"Auth cookie exists in jar but is not being transmitted for host "
                f"'{hostname}'. Localhost workaround is disabled; authenticated "
                f"session cannot be established."
            )

        # Cookie exists in jar but won't be sent — likely a localhost
        # domain-matching issue.
        logger.warning(
            "Auth cookie exists in jar but is not being transmitted "
            "(localhost cookie-jar bug). Applying workaround…"
        )
        logger.debug(
            "Cookie domains in jar: %s",
            [(c.name, c.domain, c.path) for c in self.session.cookies],
        )

        fixed = _rebind_localhost_cookies(self.session)
        if fixed:
            logger.info(
                "Fixed %d cookie(s) with localhost domain: %s",
                len(fixed),
                ", ".join(fixed),
            )

        # Re-extract XSRF token from the (possibly updated) cookie jar
        self._xsrf_token = self._extract_xsrf_token()

        # Verify the fix worked
        if not self._sends_auth_cookie():
            # Avoid logging raw cookie values; include only non-sensitive metadata.
            cookie_names = [getattr(c, "name", "") for c in self.session.cookies]
            raise GerritAuthError(
                f"Auth cookie for account {account_id} is not being "
                f"transmitted even after localhost workaround. "
                f"Cookie jar contains {len(cookie_names)} cookie(s): "
                f"{', '.join(cookie_names)}",
            )

        self._confirm_authenticated_account(account_id)

    def _confirm_authenticated_account(self, account_id: int) -> None:
        """Call ``accounts/self`` and confirm Gerrit accepted the session.

        This is the end-to-end confirmation performed after the localhost
        cookie workaround: the cookie is demonstrably being sent, but we
        still need Gerrit to accept it *and* to report the account we
        asked to become.

        Args:
            account_id: The account ID the session is expected to hold.

        Raises:
            GerritAuthError: If Gerrit rejects the cookie, omits
                ``_account_id``, or reports a different account.
        """
        try:
            account_info = self.get("accounts/self")
            actual_id = account_info.get("_account_id")
            if actual_id is None:
                raise GerritAuthError(
                    f"Auth cookie is transmitted but accounts/self response "
                    f"does not contain _account_id for requested account {account_id}."
                )
            if int(actual_id) != int(account_id):
                raise GerritAuthError(
                    f"Auth cookie authenticated as unexpected account {actual_id} "
                    f"instead of requested account {account_id}."
                )
            logger.debug(
                "Auth verification: accounts/self returned ID %s (expected %s) ✅",
                actual_id,
                account_id,
            )
        except GerritAuthError as exc:
            raise GerritAuthError(
                f"Auth cookie is transmitted but Gerrit rejected it "
                f"for account {account_id}: {exc}"
            ) from exc
