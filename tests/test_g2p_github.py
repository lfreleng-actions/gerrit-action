# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Tests for the g2p_github module."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

from g2p_config import (
    G2PConfig,
)
from g2p_github import (
    GITHUB_API_BASE,
    REQUIRED_WORKFLOW_INPUTS,
    G2PCheckResult,
    _filter_workflows,
    _github_request,
    _graphql_query,
    check_github_config,
    check_magic_repo,
    check_org_access,
    check_repos_exist,
    check_token_valid,
    check_workflows,
    format_check_results,
    results_to_json,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_urlopen_response(
    status: int = 200,
    body: dict[str, Any] | list[Any] | str = "",
) -> MagicMock:
    """Create a mock for urllib.request.urlopen context manager."""
    if isinstance(body, (dict, list)):
        raw_bytes = json.dumps(body).encode("utf-8")
    else:
        raw_bytes = body.encode("utf-8")

    resp = MagicMock()
    resp.status = status
    resp.read.return_value = raw_bytes
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _make_http_error(
    status: int,
    body: dict[str, Any] | str = "",
) -> Exception:
    """Create a mock HTTPError."""
    from urllib.error import HTTPError

    if isinstance(body, dict):
        raw_bytes = json.dumps(body).encode("utf-8")
    else:
        raw_bytes = body.encode("utf-8")

    err = HTTPError(
        url="https://api.github.com/test",
        code=status,
        msg=f"HTTP {status}",
        hdrs=MagicMock(),
        fp=None,
    )
    err.read = MagicMock(return_value=raw_bytes)  # type: ignore[method-assign]
    return err


def _minimal_config(**overrides: Any) -> G2PConfig:
    """Build a minimal enabled G2PConfig with optional overrides."""
    defaults: dict[str, Any] = {
        "enabled": True,
        "github_owner": "test-org",
        "github_token": "ghp_testtoken123",
        "validation_mode": "warn",
        "validate_workflows": True,
        "validate_repos": [],
    }
    defaults.update(overrides)
    return G2PConfig(**defaults)


# ===================================================================
# G2PCheckResult
# ===================================================================


class TestG2PCheckResult:
    """Tests for the G2PCheckResult dataclass."""

    def test_defaults(self) -> None:
        r = G2PCheckResult(check_name="test", passed=True, message="ok")
        assert r.severity == "error"
        assert r.details == {}

    def test_str_passed(self) -> None:
        r = G2PCheckResult(
            check_name="test",
            passed=True,
            message="all good",
            severity="info",
        )
        s = str(r)
        assert "✅" in s
        assert "info" in s
        assert "test" in s

    def test_str_failed(self) -> None:
        r = G2PCheckResult(
            check_name="test",
            passed=False,
            message="bad",
            severity="error",
        )
        s = str(r)
        assert "❌" in s
        assert "error" in s

    def test_details_stored(self) -> None:
        r = G2PCheckResult(
            check_name="t",
            passed=True,
            message="m",
            details={"key": "value"},
        )
        assert r.details["key"] == "value"


# ===================================================================
# _github_request
# ===================================================================


class TestGithubRequest:
    """Tests for the low-level HTTP helper."""

    @patch("g2p_github.urlopen")
    def test_successful_json_response(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _make_urlopen_response(200, {"login": "bot"})
        status, data = _github_request(f"{GITHUB_API_BASE}/user", "ghp_tok")
        assert status == 200
        assert isinstance(data, dict)
        assert data["login"] == "bot"

    @patch("g2p_github.urlopen")
    def test_successful_list_response(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _make_urlopen_response(200, [{"id": 1}, {"id": 2}])
        status, data = _github_request(f"{GITHUB_API_BASE}/repos", "ghp_tok")
        assert status == 200
        assert isinstance(data, list)
        assert len(data) == 2

    @patch("g2p_github.urlopen")
    def test_non_json_response(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _make_urlopen_response(200, "plain text")
        status, data = _github_request(f"{GITHUB_API_BASE}/test", "ghp_tok")
        assert status == 200
        assert data == "plain text"

    @patch("g2p_github.urlopen")
    def test_http_error_json_body(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _make_http_error(401, {"message": "Bad credentials"})
        status, data = _github_request(f"{GITHUB_API_BASE}/user", "ghp_bad")
        assert status == 401
        assert isinstance(data, dict)
        assert data["message"] == "Bad credentials"

    @patch("g2p_github.urlopen")
    def test_http_error_text_body(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _make_http_error(500, "Server Error")
        status, data = _github_request(f"{GITHUB_API_BASE}/test", "ghp_tok")
        assert status == 500
        assert data == "Server Error"

    @patch("g2p_github.urlopen")
    def test_sets_auth_header(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _make_urlopen_response(200, {})
        _github_request(f"{GITHUB_API_BASE}/user", "ghp_mytoken")
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer ghp_mytoken"

    @patch("g2p_github.urlopen")
    def test_sets_api_version_header(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _make_urlopen_response(200, {})
        _github_request(f"{GITHUB_API_BASE}/user", "ghp_tok")
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("X-github-api-version") == "2022-11-28"

    @patch("g2p_github.urlopen")
    def test_post_with_body(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _make_urlopen_response(200, {"ok": True})
        body = json.dumps({"query": "test"}).encode("utf-8")
        status, data = _github_request(
            f"{GITHUB_API_BASE}/graphql",
            "ghp_tok",
            method="POST",
            body=body,
        )
        assert status == 200
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Content-type") == "application/json"
        assert req.method == "POST"


# ===================================================================
# _graphql_query
# ===================================================================


class TestGraphqlQuery:
    """Tests for the GraphQL helper."""

    @patch("g2p_github.urlopen")
    def test_successful_query(self, mock_urlopen: MagicMock) -> None:
        response_data = {
            "data": {"organization": {"repositories": {"nodes": [{"name": "repo1"}]}}}
        }
        mock_urlopen.return_value = _make_urlopen_response(200, response_data)
        status, data = _graphql_query("ghp_tok", "query { test }")
        assert status == 200
        assert "data" in data

    @patch("g2p_github.urlopen")
    def test_with_variables(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _make_urlopen_response(200, {"data": {}})
        _graphql_query(
            "ghp_tok",
            "query($v: String!) { test(v: $v) }",
            variables={"v": "val"},
        )
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert "variables" in body
        assert body["variables"]["v"] == "val"

    @patch("g2p_github.urlopen")
    def test_non_dict_response(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _make_urlopen_response(200, "not json dict")
        status, data = _graphql_query("ghp_tok", "query { test }")
        assert status == 200
        assert "raw" in data

    @patch("g2p_github.urlopen")
    def test_error_response(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _make_http_error(401, {"message": "Unauthorized"})
        status, data = _graphql_query("ghp_tok", "query { test }")
        assert status == 401


# ===================================================================
# check_token_valid
# ===================================================================


class TestCheckTokenValid:
    """Tests for token validation."""

    @patch("g2p_github.urlopen")
    def test_valid_token(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _make_urlopen_response(200, {"login": "bot-user"})
        r = check_token_valid("ghp_good")
        assert r.passed is True
        assert r.check_name == "token_valid"
        assert "bot-user" in r.message
        assert r.details.get("login") == "bot-user"

    @patch("g2p_github.urlopen")
    def test_invalid_token_401(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _make_http_error(401, {"message": "Bad credentials"})
        r = check_token_valid("ghp_bad")
        assert r.passed is False
        assert r.severity == "error"
        assert "401" in r.message

    @patch("g2p_github.urlopen")
    def test_invalid_token_403(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _make_http_error(403, {"message": "Forbidden"})
        r = check_token_valid("ghp_forbidden")
        assert r.passed is False
        assert "403" in r.message

    @patch("g2p_github.urlopen")
    def test_network_error(self, mock_urlopen: MagicMock) -> None:
        from urllib.error import URLError

        mock_urlopen.side_effect = URLError("Connection refused")
        r = check_token_valid("ghp_tok")
        assert r.passed is False
        assert "Network error" in r.message

    @patch("g2p_github.urlopen")
    def test_non_dict_200_response(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _make_urlopen_response(200, "not a dict")
        r = check_token_valid("ghp_tok")
        assert r.passed is True
        assert "unknown" in r.message


# ===================================================================
# check_org_access
# ===================================================================


class TestCheckOrgAccess:
    """Tests for organisation access validation."""

    @patch("g2p_github.urlopen")
    def test_org_exists(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _make_urlopen_response(
            200, {"login": "onap", "type": "Organization"}
        )
        r = check_org_access("ghp_tok", "onap")
        assert r.passed is True
        assert r.check_name == "org_access"
        assert "onap" in r.message

    @patch("g2p_github.urlopen")
    def test_org_not_found_but_user_exists(self, mock_urlopen: MagicMock) -> None:
        # First call (orgs) → 404, second call (users) → 200
        mock_urlopen.side_effect = [
            _make_http_error(404, {"message": "Not Found"}),
            _make_urlopen_response(200, {"login": "myuser", "type": "User"}),
        ]
        r = check_org_access("ghp_tok", "myuser")
        assert r.passed is True
        assert "user account" in r.message
        assert r.details.get("account_type") == "user"

    @patch("g2p_github.urlopen")
    def test_neither_org_nor_user(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = [
            _make_http_error(404, {"message": "Not Found"}),
            _make_http_error(404, {"message": "Not Found"}),
        ]
        r = check_org_access("ghp_tok", "nonexistent")
        assert r.passed is False
        assert "404" in r.message

    @patch("g2p_github.urlopen")
    def test_forbidden(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _make_http_error(403, "Forbidden")
        r = check_org_access("ghp_tok", "secret-org")
        assert r.passed is False
        assert "403" in r.message

    @patch("g2p_github.urlopen")
    def test_network_error(self, mock_urlopen: MagicMock) -> None:
        from urllib.error import URLError

        mock_urlopen.side_effect = URLError("DNS failure")
        r = check_org_access("ghp_tok", "onap")
        assert r.passed is False
        assert "Network error" in r.message

    @patch("g2p_github.urlopen")
    def test_user_check_network_error_fallback(self, mock_urlopen: MagicMock) -> None:
        from urllib.error import URLError

        # Org check returns 404, user check has network error
        mock_urlopen.side_effect = [
            _make_http_error(404, {"message": "Not Found"}),
            URLError("timeout"),
        ]
        r = check_org_access("ghp_tok", "flaky")
        assert r.passed is False
        assert "404" in r.message


# ===================================================================
# check_magic_repo
# ===================================================================


class TestCheckMagicRepo:
    """Tests for .github magic repository validation."""

    @patch("g2p_github.urlopen")
    def test_repo_exists(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _make_urlopen_response(
            200, {"name": ".github", "full_name": "onap/.github"}
        )
        r = check_magic_repo("ghp_tok", "onap")
        assert r.passed is True
        assert r.check_name == "magic_repo"
        assert ".github" in r.message

    @patch("g2p_github.urlopen")
    def test_repo_not_found(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _make_http_error(404, {"message": "Not Found"})
        r = check_magic_repo("ghp_tok", "new-org")
        assert r.passed is False
        assert r.severity == "warning"
        assert "not found" in r.message

    @patch("g2p_github.urlopen")
    def test_network_error(self, mock_urlopen: MagicMock) -> None:
        from urllib.error import URLError

        mock_urlopen.side_effect = URLError("timeout")
        r = check_magic_repo("ghp_tok", "onap")
        assert r.passed is False
        assert r.severity == "warning"


# ===================================================================
# _filter_workflows
# ===================================================================


class TestFilterWorkflows:
    """Tests for workflow filtering logic."""

    def test_matches_gerrit_verify(self) -> None:
        workflows = [
            {"path": ".github/workflows/gerrit-verify.yaml", "state": "active"},
            {"path": ".github/workflows/build.yaml", "state": "active"},
        ]
        result = _filter_workflows(workflows, "verify")
        assert len(result) == 1
        assert "gerrit-verify" in result[0]["path"]

    def test_matches_gerrit_merge(self) -> None:
        workflows = [
            {"path": ".github/workflows/gerrit-merge.yaml", "state": "active"},
            {"path": ".github/workflows/gerrit-verify.yaml", "state": "active"},
        ]
        result = _filter_workflows(workflows, "merge")
        assert len(result) == 1

    def test_case_insensitive(self) -> None:
        workflows = [
            {"path": ".github/workflows/Gerrit-Verify.yaml", "state": "active"},
        ]
        result = _filter_workflows(workflows, "verify")
        assert len(result) == 1

    def test_skips_inactive(self) -> None:
        workflows = [
            {"path": ".github/workflows/gerrit-verify.yaml", "state": "disabled"},
        ]
        result = _filter_workflows(workflows, "verify")
        assert len(result) == 0

    def test_skips_non_gerrit(self) -> None:
        workflows = [
            {"path": ".github/workflows/ci-verify.yaml", "state": "active"},
        ]
        result = _filter_workflows(workflows, "verify")
        assert len(result) == 0

    def test_multiple_matches(self) -> None:
        workflows = [
            {"path": ".github/workflows/gerrit-verify.yaml", "state": "active"},
            {
                "path": ".github/workflows/gerrit-required-verify.yaml",
                "state": "active",
            },
        ]
        result = _filter_workflows(workflows, "verify")
        assert len(result) == 2

    def test_empty_workflows(self) -> None:
        result = _filter_workflows([], "verify")
        assert result == []

    def test_missing_path_field(self) -> None:
        workflows = [{"state": "active"}]
        result = _filter_workflows(workflows, "verify")
        assert result == []

    def test_required_workflows(self) -> None:
        workflows = [
            {
                "path": ".github/workflows/gerrit-required-verify.yaml",
                "state": "active",
            },
            {"path": ".github/workflows/gerrit-required-merge.yaml", "state": "active"},
        ]
        verify = _filter_workflows(workflows, "verify")
        merge = _filter_workflows(workflows, "merge")
        assert len(verify) == 1
        assert len(merge) == 1


# ===================================================================
# check_workflows
# ===================================================================


class TestCheckWorkflows:
    """Tests for workflow validation."""

    @patch("g2p_github.urlopen")
    def test_workflows_found(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _make_urlopen_response(
            200,
            {
                "total_count": 2,
                "workflows": [
                    {"path": ".github/workflows/gerrit-verify.yaml", "state": "active"},
                    {"path": ".github/workflows/build.yaml", "state": "active"},
                ],
            },
        )
        r = check_workflows("ghp_tok", "onap", ".github", "verify")
        assert r.passed is True
        assert "1" in r.message
        assert r.check_name == "workflows_.github_verify"

    @patch("g2p_github.urlopen")
    def test_no_matching_workflows(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _make_urlopen_response(
            200,
            {
                "total_count": 1,
                "workflows": [
                    {"path": ".github/workflows/build.yaml", "state": "active"},
                ],
            },
        )
        r = check_workflows("ghp_tok", "onap", ".github", "verify")
        assert r.passed is False
        assert r.severity == "warning"

    @patch("g2p_github.urlopen")
    def test_repo_not_found(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _make_http_error(404, "Not Found")
        r = check_workflows("ghp_tok", "onap", "missing-repo", "verify")
        assert r.passed is False
        assert "404" in r.message

    @patch("g2p_github.urlopen")
    def test_network_error(self, mock_urlopen: MagicMock) -> None:
        from urllib.error import URLError

        mock_urlopen.side_effect = URLError("timeout")
        r = check_workflows("ghp_tok", "onap", ".github", "verify")
        assert r.passed is False
        assert "Network error" in r.message

    @patch("g2p_github.urlopen")
    def test_check_name_includes_repo_and_filter(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _make_urlopen_response(200, {"workflows": []})
        r = check_workflows("ghp_tok", "org", "ci-management", "merge")
        assert r.check_name == "workflows_ci-management_merge"

    @patch("g2p_github.urlopen")
    def test_unexpected_response_format(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _make_urlopen_response(200, "not a dict")
        r = check_workflows("ghp_tok", "onap", ".github", "verify")
        assert r.passed is False
        assert "Unexpected" in r.message

    @patch("g2p_github.urlopen")
    def test_empty_workflows_list(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _make_urlopen_response(200, {"workflows": []})
        r = check_workflows("ghp_tok", "onap", ".github", "verify")
        assert r.passed is False
        assert r.details.get("total_workflows") == 0


# ===================================================================
# check_repos_exist
# ===================================================================


class TestCheckReposExist:
    """Tests for GraphQL repository existence check."""

    def test_empty_repos_list(self) -> None:
        r = check_repos_exist("ghp_tok", "onap", [])
        assert r.passed is True
        assert r.check_name == "repos_exist"

    @patch("g2p_github.urlopen")
    def test_all_repos_found(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _make_urlopen_response(
            200,
            {
                "data": {
                    "organization": {
                        "repositories": {
                            "nodes": [
                                {"name": "ci-management", "isArchived": False},
                                {"name": "releng-lftools", "isArchived": False},
                            ]
                        }
                    }
                }
            },
        )
        r = check_repos_exist("ghp_tok", "onap", ["ci-management", "releng-lftools"])
        assert r.passed is True
        assert "2" in r.message

    @patch("g2p_github.urlopen")
    def test_missing_repos(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _make_urlopen_response(
            200,
            {
                "data": {
                    "organization": {
                        "repositories": {
                            "nodes": [
                                {"name": "ci-management", "isArchived": False},
                            ]
                        }
                    }
                }
            },
        )
        r = check_repos_exist("ghp_tok", "onap", ["ci-management", "nonexistent"])
        assert r.passed is False
        assert "nonexistent" in r.message
        assert "nonexistent" in r.details["missing"]

    @patch("g2p_github.urlopen")
    def test_archived_repos_noted(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _make_urlopen_response(
            200,
            {
                "data": {
                    "organization": {
                        "repositories": {
                            "nodes": [
                                {"name": "old-repo", "isArchived": True},
                            ]
                        }
                    }
                }
            },
        )
        r = check_repos_exist("ghp_tok", "onap", ["old-repo"])
        assert r.passed is True
        assert "archived" in r.message.lower()
        assert "old-repo" in r.details["archived"]

    @patch("g2p_github.urlopen")
    def test_graphql_error(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _make_urlopen_response(
            200,
            {
                "errors": [{"message": "Field not found"}],
            },
        )
        r = check_repos_exist("ghp_tok", "onap", ["repo1"])
        assert r.passed is False
        assert "GraphQL errors" in r.message

    @patch("g2p_github.urlopen")
    def test_org_not_found_in_graphql(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _make_urlopen_response(
            200,
            {
                "data": {"organization": None},
            },
        )
        r = check_repos_exist("ghp_tok", "missing-org", ["repo1"])
        assert r.passed is False
        assert "not found" in r.message

    @patch("g2p_github.urlopen")
    def test_http_error(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _make_http_error(401, "Unauthorized")
        r = check_repos_exist("ghp_tok", "onap", ["repo1"])
        assert r.passed is False
        assert "401" in r.message

    @patch("g2p_github.urlopen")
    def test_network_error(self, mock_urlopen: MagicMock) -> None:
        from urllib.error import URLError

        mock_urlopen.side_effect = URLError("Connection refused")
        r = check_repos_exist("ghp_tok", "onap", ["repo1"])
        assert r.passed is False
        assert "Network error" in r.message

    @patch("g2p_github.urlopen")
    def test_null_nodes_handled(self, mock_urlopen: MagicMock) -> None:
        """Handle null entries in the nodes array gracefully."""
        mock_urlopen.return_value = _make_urlopen_response(
            200,
            {
                "data": {
                    "organization": {
                        "repositories": {
                            "nodes": [
                                None,
                                {"name": "repo1", "isArchived": False},
                            ]
                        }
                    }
                }
            },
        )
        r = check_repos_exist("ghp_tok", "onap", ["repo1"])
        assert r.passed is True


# ===================================================================
# check_github_config (aggregate runner)
# ===================================================================


class TestCheckGithubConfig:
    """Tests for the aggregate check runner."""

    def test_no_token_returns_single_warning(self) -> None:
        config = _minimal_config(github_token="")
        results = check_github_config(config)
        assert len(results) == 1
        assert results[0].check_name == "token_provided"
        assert results[0].passed is False
        assert results[0].severity == "warning"

    @patch("g2p_github.urlopen")
    def test_invalid_token_stops_early(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _make_http_error(401, {"message": "Bad credentials"})
        config = _minimal_config()
        results = check_github_config(config)
        # Should have: token_provided (pass), token_valid (fail)
        assert len(results) == 2
        assert results[0].check_name == "token_provided"
        assert results[0].passed is True
        assert results[1].check_name == "token_valid"
        assert results[1].passed is False

    @patch("g2p_github.urlopen")
    def test_org_not_found_stops_early(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = [
            # token check
            _make_urlopen_response(200, {"login": "bot"}),
            # org check (404) and user fallback (404)
            _make_http_error(404, "Not Found"),
            _make_http_error(404, "Not Found"),
        ]
        config = _minimal_config()
        results = check_github_config(config)
        # token_provided, token_valid, org_access
        assert len(results) == 3
        assert results[2].check_name == "org_access"
        assert results[2].passed is False

    @patch("g2p_github.urlopen")
    def test_full_pass_with_workflows(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = [
            # token check
            _make_urlopen_response(200, {"login": "bot"}),
            # org check
            _make_urlopen_response(200, {"login": "onap"}),
            # magic repo check
            _make_urlopen_response(200, {"name": ".github"}),
            # .github verify workflows
            _make_urlopen_response(
                200,
                {
                    "workflows": [
                        {
                            "path": ".github/workflows/gerrit-verify.yaml",
                            "state": "active",
                        },
                    ]
                },
            ),
            # .github merge workflows
            _make_urlopen_response(
                200,
                {
                    "workflows": [
                        {
                            "path": ".github/workflows/gerrit-merge.yaml",
                            "state": "active",
                        },
                    ]
                },
            ),
        ]
        config = _minimal_config(validate_repos=[])
        results = check_github_config(config)
        # token_provided, token_valid, org_access, magic_repo,
        # workflows_verify, workflows_merge
        assert len(results) == 6
        assert all(r.passed for r in results)

    @patch("g2p_github.urlopen")
    def test_validate_repos_triggers_per_repo_checks(
        self, mock_urlopen: MagicMock
    ) -> None:
        mock_urlopen.side_effect = [
            # token check
            _make_urlopen_response(200, {"login": "bot"}),
            # org check
            _make_urlopen_response(200, {"login": "onap"}),
            # magic repo
            _make_urlopen_response(200, {"name": ".github"}),
            # .github verify
            _make_urlopen_response(
                200,
                {
                    "workflows": [
                        {
                            "path": ".github/workflows/gerrit-verify.yaml",
                            "state": "active",
                        },
                    ]
                },
            ),
            # .github merge
            _make_urlopen_response(
                200,
                {
                    "workflows": [
                        {
                            "path": ".github/workflows/gerrit-merge.yaml",
                            "state": "active",
                        },
                    ]
                },
            ),
            # ci-management verify
            _make_urlopen_response(
                200,
                {
                    "workflows": [
                        {
                            "path": ".github/workflows/gerrit-verify.yaml",
                            "state": "active",
                        },
                    ]
                },
            ),
            # ci-management merge
            _make_urlopen_response(200, {"workflows": []}),
            # repos_exist GraphQL
            _make_urlopen_response(
                200,
                {
                    "data": {
                        "organization": {
                            "repositories": {
                                "nodes": [
                                    {"name": "ci-management", "isArchived": False}
                                ]
                            }
                        }
                    },
                },
            ),
        ]
        config = _minimal_config(validate_repos=["ci-management"])
        results = check_github_config(config)
        check_names = [r.check_name for r in results]
        assert "workflows_ci-management_verify" in check_names
        assert "workflows_ci-management_merge" in check_names
        assert "repos_exist" in check_names

    @patch("g2p_github.urlopen")
    def test_validate_workflows_false_skips_workflow_checks(
        self, mock_urlopen: MagicMock
    ) -> None:
        mock_urlopen.side_effect = [
            # token check
            _make_urlopen_response(200, {"login": "bot"}),
            # org check
            _make_urlopen_response(200, {"login": "onap"}),
            # magic repo
            _make_urlopen_response(200, {"name": ".github"}),
        ]
        config = _minimal_config(validate_workflows=False, validate_repos=[])
        results = check_github_config(config)
        check_names = [r.check_name for r in results]
        assert not any("workflows_" in n for n in check_names)


# ===================================================================
# format_check_results
# ===================================================================


class TestFormatCheckResults:
    """Tests for result formatting."""

    def test_all_passed_no_annotations(self) -> None:
        results = [
            G2PCheckResult("a", True, "ok", "info"),
            G2PCheckResult("b", True, "ok", "info"),
        ]
        annotations, has_fatal = format_check_results(results, "error")
        assert annotations == []
        assert has_fatal is False

    def test_error_mode_with_error_severity(self) -> None:
        results = [
            G2PCheckResult("token_valid", False, "Token bad", "error"),
        ]
        annotations, has_fatal = format_check_results(results, "error")
        assert has_fatal is True
        assert any("::error::" in a for a in annotations)

    def test_warn_mode_with_error_severity(self) -> None:
        results = [
            G2PCheckResult("token_valid", False, "Token bad", "error"),
        ]
        annotations, has_fatal = format_check_results(results, "warn")
        assert has_fatal is False
        assert any("::warning::" in a for a in annotations)

    def test_warning_severity_always_warning(self) -> None:
        results = [
            G2PCheckResult("magic_repo", False, "Missing", "warning"),
        ]
        annotations, has_fatal = format_check_results(results, "error")
        assert has_fatal is False
        assert any("::warning::" in a for a in annotations)

    def test_info_severity_not_annotated(self) -> None:
        results = [
            G2PCheckResult("repos_exist", False, "Not found", "info"),
        ]
        annotations, has_fatal = format_check_results(results, "error")
        assert annotations == []
        assert has_fatal is False

    def test_mixed_results(self) -> None:
        results = [
            G2PCheckResult("a", True, "pass", "info"),
            G2PCheckResult("b", False, "warn msg", "warning"),
            G2PCheckResult("c", False, "err msg", "error"),
        ]
        annotations, has_fatal = format_check_results(results, "error")
        assert has_fatal is True
        assert len(annotations) == 2  # warning + error

    def test_empty_results(self) -> None:
        annotations, has_fatal = format_check_results([], "error")
        assert annotations == []
        assert has_fatal is False


# ===================================================================
# results_to_json
# ===================================================================


class TestResultsToJson:
    """Tests for JSON serialisation of results."""

    def test_serialises_results(self) -> None:
        results = [
            G2PCheckResult("a", True, "ok", "info"),
            G2PCheckResult("b", False, "bad", "error"),
        ]
        raw = results_to_json(results)
        data = json.loads(raw)
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["check_name"] == "a"
        assert data[0]["passed"] is True
        assert data[1]["passed"] is False

    def test_empty_results(self) -> None:
        raw = results_to_json([])
        data = json.loads(raw)
        assert data == []

    def test_fields_present(self) -> None:
        results = [
            G2PCheckResult("test", True, "msg", "warning"),
        ]
        raw = results_to_json(results)
        data = json.loads(raw)
        item = data[0]
        assert "check_name" in item
        assert "passed" in item
        assert "message" in item
        assert "severity" in item
        # details should NOT be in the serialised output
        assert "details" not in item


# ===================================================================
# Constants validation
# ===================================================================


class TestConstants:
    """Verify module-level constants."""

    def test_required_workflow_inputs(self) -> None:
        assert isinstance(REQUIRED_WORKFLOW_INPUTS, tuple)
        assert len(REQUIRED_WORKFLOW_INPUTS) == 9
        assert all(inp.startswith("GERRIT_") for inp in REQUIRED_WORKFLOW_INPUTS)

    def test_github_api_base(self) -> None:
        assert GITHUB_API_BASE == "https://api.github.com"

    def test_required_inputs_include_key_fields(self) -> None:
        assert "GERRIT_BRANCH" in REQUIRED_WORKFLOW_INPUTS
        assert "GERRIT_PROJECT" in REQUIRED_WORKFLOW_INPUTS
        assert "GERRIT_REFSPEC" in REQUIRED_WORKFLOW_INPUTS
        assert "GERRIT_CHANGE_ID" in REQUIRED_WORKFLOW_INPUTS
        assert "GERRIT_EVENT_TYPE" in REQUIRED_WORKFLOW_INPUTS
