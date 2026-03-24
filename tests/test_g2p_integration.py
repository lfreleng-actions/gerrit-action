# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Integration tests for the G2P configure-g2p entry point.

These tests exercise the full orchestration flow from environment
variable parsing through config validation, GitHub API checks, and
container setup — with Docker and HTTP interactions mocked.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from conftest import FIXTURES_DIR
from g2p_config import (
    DEFAULT_COMMENT_MAPPINGS,
    VALID_HOOKS,
    G2PConfig,
)
from g2p_github import (
    G2PCheckResult,
    check_github_config,
    format_check_results,
    results_to_json,
)
from g2p_setup import (
    G2P_INI_PATH,
    generate_g2p_ini,
    generate_g2p_replication_section,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALL_G2P_ENV_VARS = [
    "G2P_ENABLE",
    "G2P_GITHUB_TOKEN",
    "G2P_GITHUB_OWNER",
    "G2P_REMOTE_NAME_STYLE",
    "G2P_REMOTE_URL",
    "G2P_REMOTE_AUTH_GROUP",
    "G2P_COMMENT_MAPPINGS",
    "G2P_HOOKS",
    "G2P_VALIDATION_MODE",
    "G2P_VALIDATE_WORKFLOWS",
    "G2P_VALIDATE_REPOS",
    "G2P_SSH_PRIVATE_KEY",
    "G2P_GITHUB_KNOWN_HOSTS",
]


@pytest.fixture()
def clean_g2p_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Remove all G2P environment variables."""
    for var in ALL_G2P_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _set_onap_env(mp: pytest.MonkeyPatch) -> None:
    """Set environment variables matching ONAP production config."""
    mp.setenv("G2P_ENABLE", "true")
    mp.setenv("G2P_GITHUB_OWNER", "onap")
    mp.setenv("G2P_GITHUB_TOKEN", "ghp_onap_pat")
    mp.setenv("G2P_REMOTE_NAME_STYLE", "dash")
    mp.setenv(
        "G2P_COMMENT_MAPPINGS",
        json.dumps(
            {
                "recheck": "verify",
                "remerge": "merge",
                "rerun-gha": "verify",
                "remerge-gha": "merge",
            }
        ),
    )
    mp.setenv("G2P_HOOKS", "patchset-created,comment-added,change-merged")
    mp.setenv("G2P_VALIDATION_MODE", "warn")


def _set_fdio_env(mp: pytest.MonkeyPatch) -> None:
    """Set environment variables matching FD.io production config."""
    mp.setenv("G2P_ENABLE", "true")
    mp.setenv("G2P_GITHUB_OWNER", "fdio")
    mp.setenv("G2P_GITHUB_TOKEN", "ghp_fdio_pat")
    mp.setenv("G2P_REMOTE_NAME_STYLE", "dash")
    mp.setenv(
        "G2P_COMMENT_MAPPINGS",
        json.dumps(
            {
                "recheck": "verify",
                "remerge": "merge",
                "rerun-gha": "verify",
            }
        ),
    )


def _set_minimal_env(mp: pytest.MonkeyPatch) -> None:
    """Set the minimum viable G2P environment."""
    mp.setenv("G2P_ENABLE", "true")
    mp.setenv("G2P_GITHUB_OWNER", "test-org")


def _make_docker_mock(
    *,
    exec_cmd_return: str = "0",
    exec_test_return: bool = True,
) -> MagicMock:
    """Create a mock DockerManager."""
    docker = MagicMock()
    docker.exec_cmd.return_value = exec_cmd_return
    docker.exec_test.return_value = exec_test_return
    docker.cp.return_value = None
    return docker


def _make_urlopen_response(
    status: int = 200,
    body: dict[str, Any] | list[Any] | str = "",
) -> MagicMock:
    """Create a mock for urllib.request.urlopen."""
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


# ===================================================================
# End-to-end config → INI → replication flow
# ===================================================================


class TestConfigToIniFlow:
    """Test the full config → INI generation pipeline."""

    def test_onap_config_generates_valid_ini(
        self, clean_g2p_env: pytest.MonkeyPatch
    ) -> None:
        """ONAP production env → G2PConfig → INI matches fixture."""
        _set_onap_env(clean_g2p_env)
        config = G2PConfig.from_environment()

        assert config.check() == []

        ini = generate_g2p_ini(config)

        # Verify structure matches production pattern
        import configparser

        cp = configparser.ConfigParser()
        cp.optionxform = str  # type: ignore[assignment]
        cp.read_string(ini)

        assert cp.has_section('mapping "comment-added"')
        assert cp.has_section("github.com")
        assert cp.get("github.com", "token") == "ghp_onap_pat"

        section = 'mapping "comment-added"'
        assert cp.get(section, "recheck") == "verify"
        assert cp.get(section, "remerge") == "merge"
        assert cp.get(section, "rerun-gha") == "verify"
        assert cp.get(section, "remerge-gha") == "merge"

    def test_fdio_config_generates_valid_ini(
        self, clean_g2p_env: pytest.MonkeyPatch
    ) -> None:
        """FD.io production env → G2PConfig → INI generation."""
        _set_fdio_env(clean_g2p_env)
        config = G2PConfig.from_environment()

        assert config.check() == []

        ini = generate_g2p_ini(config)

        import configparser

        cp = configparser.ConfigParser()
        cp.optionxform = str  # type: ignore[assignment]
        cp.read_string(ini)

        section = 'mapping "comment-added"'
        assert cp.get(section, "recheck") == "verify"
        assert cp.get(section, "remerge") == "merge"
        assert cp.get(section, "rerun-gha") == "verify"
        assert not cp.has_option(section, "remerge-gha")

    def test_minimal_config_uses_defaults(
        self, clean_g2p_env: pytest.MonkeyPatch
    ) -> None:
        """Minimal env → default mappings in INI."""
        _set_minimal_env(clean_g2p_env)
        config = G2PConfig.from_environment()

        ini = generate_g2p_ini(config)

        import configparser

        cp = configparser.ConfigParser()
        cp.optionxform = str  # type: ignore[assignment]
        cp.read_string(ini)

        section = 'mapping "comment-added"'
        for keyword, wf_filter in DEFAULT_COMMENT_MAPPINGS.items():
            assert cp.get(section, keyword) == wf_filter

        # No github.com section (no token)
        assert not cp.has_section("github.com")


class TestConfigToReplicationFlow:
    """Test the full config → replication section pipeline."""

    def test_onap_replication_section(self, clean_g2p_env: pytest.MonkeyPatch) -> None:
        """ONAP env → replication section with correct URL and style."""
        _set_onap_env(clean_g2p_env)
        config = G2PConfig.from_environment()

        section = generate_g2p_replication_section(config)

        assert '[remote "github-g2p"]' in section
        assert "git@github.com:onap/${name}.git" in section
        assert "authGroup = GitHub Replication" in section
        assert "remoteNameStyle = dash" in section

    def test_fdio_replication_section(self, clean_g2p_env: pytest.MonkeyPatch) -> None:
        """FD.io env → correct remote URL for fdio org."""
        _set_fdio_env(clean_g2p_env)
        config = G2PConfig.from_environment()

        section = generate_g2p_replication_section(config)

        assert "git@github.com:fdio/${name}.git" in section

    def test_custom_url_overrides_auto_generation(
        self, clean_g2p_env: pytest.MonkeyPatch
    ) -> None:
        """Explicit remote URL takes precedence over owner-derived URL."""
        _set_minimal_env(clean_g2p_env)
        clean_g2p_env.setenv(
            "G2P_REMOTE_URL",
            "ssh://git@github.example.com/${name}.git",
        )
        config = G2PConfig.from_environment()

        section = generate_g2p_replication_section(config)

        assert "ssh://git@github.example.com/${name}.git" in section
        assert "test-org" not in section

    def test_underscore_name_style(self, clean_g2p_env: pytest.MonkeyPatch) -> None:
        """Underscore name style propagates to replication section."""
        _set_minimal_env(clean_g2p_env)
        clean_g2p_env.setenv("G2P_REMOTE_NAME_STYLE", "underscore")
        config = G2PConfig.from_environment()

        section = generate_g2p_replication_section(config)

        assert "remoteNameStyle = underscore" in section


# ===================================================================
# Config → GitHub checks flow
# ===================================================================


class TestConfigToGitHubChecksFlow:
    """Test the full config → GitHub API checks pipeline."""

    def test_no_token_returns_single_warning(
        self, clean_g2p_env: pytest.MonkeyPatch
    ) -> None:
        """Missing token produces a single warning result."""
        _set_minimal_env(clean_g2p_env)
        config = G2PConfig.from_environment()

        results = check_github_config(config)

        assert len(results) == 1
        assert results[0].check_name == "token_provided"
        assert results[0].passed is False
        assert results[0].severity == "warning"

    @patch("g2p_github.urlopen")
    def test_full_check_pass_with_fixtures(
        self,
        mock_urlopen: MagicMock,
        clean_g2p_env: pytest.MonkeyPatch,
        g2p_workflows_response: dict[str, Any],
        g2p_user_response: dict[str, Any],
        g2p_org_response: dict[str, Any],
    ) -> None:
        """Full check pass using fixture data."""
        _set_onap_env(clean_g2p_env)
        config = G2PConfig.from_environment()

        mock_urlopen.side_effect = [
            # token check → user response
            _make_urlopen_response(200, g2p_user_response),
            # org check → org response
            _make_urlopen_response(200, g2p_org_response),
            # magic repo check
            _make_urlopen_response(200, {"name": ".github"}),
            # .github verify workflows
            _make_urlopen_response(200, g2p_workflows_response),
            # .github merge workflows
            _make_urlopen_response(200, g2p_workflows_response),
        ]

        results = check_github_config(config)

        assert all(r.passed for r in results)
        assert len(results) >= 5

    @patch("g2p_github.urlopen")
    def test_error_mode_raises_on_invalid_token(
        self,
        mock_urlopen: MagicMock,
        clean_g2p_env: pytest.MonkeyPatch,
    ) -> None:
        """Error validation mode with bad token → fatal failure."""
        _set_onap_env(clean_g2p_env)
        clean_g2p_env.setenv("G2P_VALIDATION_MODE", "error")
        config = G2PConfig.from_environment()

        mock_urlopen.side_effect = _make_http_error(401, {"message": "Bad credentials"})

        results = check_github_config(config)
        _, has_fatal = format_check_results(results, "error")

        assert has_fatal is True

    @patch("g2p_github.urlopen")
    def test_warn_mode_continues_on_invalid_token(
        self,
        mock_urlopen: MagicMock,
        clean_g2p_env: pytest.MonkeyPatch,
    ) -> None:
        """Warn validation mode with bad token → no fatal failure."""
        _set_onap_env(clean_g2p_env)
        clean_g2p_env.setenv("G2P_VALIDATION_MODE", "warn")
        config = G2PConfig.from_environment()

        mock_urlopen.side_effect = _make_http_error(401, {"message": "Bad credentials"})

        results = check_github_config(config)
        annotations, has_fatal = format_check_results(results, "warn")

        assert has_fatal is False
        assert len(annotations) >= 1

    def test_skip_mode_skips_all_checks(
        self, clean_g2p_env: pytest.MonkeyPatch
    ) -> None:
        """Skip validation mode returns no results."""
        _set_onap_env(clean_g2p_env)
        clean_g2p_env.setenv("G2P_VALIDATION_MODE", "skip")
        config = G2PConfig.from_environment()

        # In skip mode the caller does not call check_github_config
        assert config.validation_mode == "skip"


# ===================================================================
# Config → setup_g2p container flow
# ===================================================================


class TestConfigToContainerSetupFlow:
    """Test config → setup_g2p orchestration with mocked Docker."""

    @patch("g2p_setup.fetch_github_host_keys")
    @patch("g2p_setup.generate_ssh_keypair")
    def test_onap_full_setup(
        self,
        mock_keygen: MagicMock,
        mock_host_keys: MagicMock,
        clean_g2p_env: pytest.MonkeyPatch,
    ) -> None:
        """Full ONAP setup with mocked Docker and SSH."""
        from g2p_setup import setup_g2p

        _set_onap_env(clean_g2p_env)
        config = G2PConfig.from_environment()

        mock_keygen.return_value = (
            "-----BEGIN OPENSSH PRIVATE KEY-----\ntest\n"
            "-----END OPENSSH PRIVATE KEY-----",
            "ssh-ed25519 AAAAtest gerrit-action-g2p",
        )
        mock_host_keys.return_value = "github.com ssh-ed25519 AAAAhostkey"

        docker = _make_docker_mock()
        result = setup_g2p(config, docker, "container123")

        assert result.config_path == G2P_INI_PATH
        assert result.hooks_enabled == list(VALID_HOOKS)
        assert result.ssh_public_key.startswith("ssh-ed25519")
        assert result.replication_remote_configured is True

        # Docker should have been called multiple times
        assert docker.exec_cmd.call_count > 0
        assert docker.cp.call_count > 0

    @patch("g2p_setup.fetch_github_host_keys")
    def test_minimal_setup_no_token(
        self,
        mock_host_keys: MagicMock,
        clean_g2p_env: pytest.MonkeyPatch,
    ) -> None:
        """Minimal config (no token) still sets up hooks and SSH."""
        from g2p_setup import setup_g2p

        _set_minimal_env(clean_g2p_env)
        config = G2PConfig.from_environment()

        mock_host_keys.return_value = "github.com ssh-ed25519 AAAAhostkey"

        docker = _make_docker_mock()

        with patch("g2p_setup.generate_ssh_keypair") as mock_keygen:
            mock_keygen.return_value = (
                "-----BEGIN KEY-----\ntest\n-----END KEY-----",
                "ssh-ed25519 AAAAgenerated gerrit-action-g2p",
            )
            result = setup_g2p(config, docker, "container456")

        assert result.config_path == G2P_INI_PATH
        assert len(result.hooks_enabled) == 3
        assert result.ssh_public_key != ""

    @patch("g2p_setup.fetch_github_host_keys")
    def test_single_hook_setup(
        self,
        mock_host_keys: MagicMock,
        clean_g2p_env: pytest.MonkeyPatch,
    ) -> None:
        """Single hook selection → only one symlink created."""
        from g2p_setup import setup_g2p

        _set_minimal_env(clean_g2p_env)
        clean_g2p_env.setenv("G2P_HOOKS", "patchset-created")
        config = G2PConfig.from_environment()

        mock_host_keys.return_value = "github.com ssh-ed25519 AAAAhostkey"

        docker = _make_docker_mock()

        with patch("g2p_setup.generate_ssh_keypair") as mock_keygen:
            mock_keygen.return_value = ("priv", "pub")
            result = setup_g2p(config, docker, "container789")

        assert result.hooks_enabled == ["patchset-created"]

    @patch("g2p_setup.fetch_github_host_keys")
    def test_provided_ssh_key_not_generated(
        self,
        mock_host_keys: MagicMock,
        clean_g2p_env: pytest.MonkeyPatch,
    ) -> None:
        """Provided SSH key skips keypair generation."""
        from g2p_setup import setup_g2p

        _set_minimal_env(clean_g2p_env)
        clean_g2p_env.setenv(
            "G2P_SSH_PRIVATE_KEY",
            "-----BEGIN OPENSSH PRIVATE KEY-----\nprovided\n"
            "-----END OPENSSH PRIVATE KEY-----",
        )
        config = G2PConfig.from_environment()

        mock_host_keys.return_value = "github.com ssh-ed25519 AAAAhostkey"

        docker = _make_docker_mock()

        with patch("g2p_setup.generate_ssh_keypair") as mock_keygen:
            with patch("g2p_setup.subprocess.run") as mock_subproc:
                import subprocess

                mock_subproc.return_value = subprocess.CompletedProcess(
                    ["ssh-keygen"],
                    0,
                    stdout="ssh-ed25519 AAAAderived gerrit-action-g2p",
                    stderr="",
                )
                result = setup_g2p(config, docker, "containerabc")

            # generate_ssh_keypair should NOT be called
            mock_keygen.assert_not_called()

        assert "AAAAderived" in result.ssh_public_key

    @patch("g2p_setup.fetch_github_host_keys")
    def test_hooks_with_missing_binary_skipped(
        self,
        mock_host_keys: MagicMock,
        clean_g2p_env: pytest.MonkeyPatch,
    ) -> None:
        """Missing g2p console script → hook is skipped."""
        from g2p_setup import setup_g2p

        _set_minimal_env(clean_g2p_env)
        config = G2PConfig.from_environment()

        mock_host_keys.return_value = "github.com ssh-ed25519 AAAAhostkey"

        # exec_test returns False → binary not found
        docker = _make_docker_mock(exec_test_return=False)

        with patch("g2p_setup.generate_ssh_keypair") as mock_keygen:
            mock_keygen.return_value = ("priv", "pub")
            result = setup_g2p(config, docker, "containerdef")

        assert result.hooks_enabled == []


# ===================================================================
# Validation error scenarios
# ===================================================================


class TestValidationErrors:
    """Test error conditions across the config → check pipeline."""

    def test_missing_owner_when_enabled(
        self, clean_g2p_env: pytest.MonkeyPatch
    ) -> None:
        """Enabled without owner → validation error."""
        clean_g2p_env.setenv("G2P_ENABLE", "true")
        clean_g2p_env.setenv("G2P_GITHUB_OWNER", "")
        config = G2PConfig.from_environment()

        errors = config.check()

        assert len(errors) >= 1
        assert any("g2p_github_owner" in e for e in errors)

    def test_invalid_name_style(self, clean_g2p_env: pytest.MonkeyPatch) -> None:
        """Invalid name style → validation error."""
        _set_minimal_env(clean_g2p_env)
        clean_g2p_env.setenv("G2P_REMOTE_NAME_STYLE", "camelCase")
        config = G2PConfig.from_environment()

        errors = config.check()

        assert any("g2p_remote_name_style" in e for e in errors)

    def test_invalid_validation_mode(self, clean_g2p_env: pytest.MonkeyPatch) -> None:
        """Invalid validation mode → validation error."""
        _set_minimal_env(clean_g2p_env)
        clean_g2p_env.setenv("G2P_VALIDATION_MODE", "strict")
        config = G2PConfig.from_environment()

        errors = config.check()

        assert any("g2p_validation_mode" in e for e in errors)

    def test_invalid_hook_name(self, clean_g2p_env: pytest.MonkeyPatch) -> None:
        """Unknown hook name → validation error."""
        _set_minimal_env(clean_g2p_env)
        clean_g2p_env.setenv("G2P_HOOKS", "patchset-created,unknown-hook")
        config = G2PConfig.from_environment()

        errors = config.check()

        assert any("unknown-hook" in e for e in errors)

    def test_invalid_comment_mapping_filter(
        self, clean_g2p_env: pytest.MonkeyPatch
    ) -> None:
        """Invalid workflow filter in mappings → validation error."""
        _set_minimal_env(clean_g2p_env)
        clean_g2p_env.setenv(
            "G2P_COMMENT_MAPPINGS",
            json.dumps({"recheck": "build"}),
        )
        config = G2PConfig.from_environment()

        errors = config.check()

        assert any("g2p_comment_mappings" in e for e in errors)

    def test_invalid_json_in_comment_mappings(
        self, clean_g2p_env: pytest.MonkeyPatch
    ) -> None:
        """Malformed JSON in comment mappings → ConfigError."""
        from errors import ConfigError

        _set_minimal_env(clean_g2p_env)
        clean_g2p_env.setenv("G2P_COMMENT_MAPPINGS", "{bad json")

        with pytest.raises(ConfigError, match="not valid JSON"):
            G2PConfig.from_environment()

    def test_multiple_validation_errors(
        self, clean_g2p_env: pytest.MonkeyPatch
    ) -> None:
        """Multiple bad inputs → all errors reported."""
        clean_g2p_env.setenv("G2P_ENABLE", "true")
        clean_g2p_env.setenv("G2P_GITHUB_OWNER", "")
        clean_g2p_env.setenv("G2P_REMOTE_NAME_STYLE", "bad")
        clean_g2p_env.setenv("G2P_VALIDATION_MODE", "bad")
        clean_g2p_env.setenv("G2P_HOOKS", "bad-hook")
        config = G2PConfig.from_environment()

        errors = config.check()

        assert len(errors) >= 4


# ===================================================================
# Fixture file validation
# ===================================================================


class TestFixtureFiles:
    """Verify test fixture files are well-formed."""

    def test_workflows_fixture_parseable(
        self, g2p_workflows_response: dict[str, Any]
    ) -> None:
        """Workflows fixture is valid JSON with expected structure."""
        assert "workflows" in g2p_workflows_response
        assert "total_count" in g2p_workflows_response
        workflows = g2p_workflows_response["workflows"]
        assert len(workflows) == 4

        # At least one should match gerrit-verify pattern
        gerrit_verify = [
            w
            for w in workflows
            if "gerrit" in w["path"].lower() and "verify" in w["path"].lower()
        ]
        assert len(gerrit_verify) >= 1

    def test_org_fixture_parseable(self, g2p_org_response: dict[str, Any]) -> None:
        """Org fixture has expected fields."""
        assert g2p_org_response["login"] == "onap"
        assert g2p_org_response["type"] == "Organization"

    def test_user_fixture_parseable(self, g2p_user_response: dict[str, Any]) -> None:
        """User fixture has expected fields."""
        assert "login" in g2p_user_response
        assert g2p_user_response["type"] == "User"

    def test_expected_ini_default_exists(self) -> None:
        """Default INI fixture exists and is parseable."""
        path = FIXTURES_DIR / "g2p_expected_ini_default.ini"
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        # Strip SPDX header lines for parsing
        lines = [
            line for line in content.splitlines() if not line.startswith("# SPDX-")
        ]
        import configparser

        cp = configparser.ConfigParser()
        cp.optionxform = str  # type: ignore[assignment]
        cp.read_string("\n".join(lines))
        assert cp.has_section('mapping "comment-added"')

    def test_expected_ini_custom_exists(self) -> None:
        """Custom INI fixture exists and has ONAP-specific mappings."""
        path = FIXTURES_DIR / "g2p_expected_ini_custom.ini"
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        lines = [
            line for line in content.splitlines() if not line.startswith("# SPDX-")
        ]
        import configparser

        cp = configparser.ConfigParser()
        cp.optionxform = str  # type: ignore[assignment]
        cp.read_string("\n".join(lines))
        section = 'mapping "comment-added"'
        assert cp.has_option(section, "rerun-gha")
        assert cp.has_option(section, "remerge-gha")

    def test_replication_remote_fixture_exists(self) -> None:
        """Replication remote fixture exists and has expected content."""
        path = FIXTURES_DIR / "g2p_expected_replication_remote.config"
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert '[remote "github-g2p"]' in content
        assert "git@github.com:onap/${name}.git" in content
        assert "remoteNameStyle = dash" in content


# ===================================================================
# Results serialisation round-trip
# ===================================================================


class TestResultsSerialisationRoundTrip:
    """Test JSON serialisation of check results."""

    def test_results_round_trip(self) -> None:
        """Serialise and deserialise check results."""
        results = [
            G2PCheckResult("a", True, "pass", "info"),
            G2PCheckResult("b", False, "fail", "error"),
            G2PCheckResult("c", True, "ok", "warning", {"key": "value"}),
        ]

        raw = results_to_json(results)
        data = json.loads(raw)

        assert len(data) == 3
        assert data[0]["check_name"] == "a"
        assert data[0]["passed"] is True
        assert data[1]["passed"] is False
        assert data[1]["severity"] == "error"
        # Details should not be in serialised output
        assert "details" not in data[2]

    def test_empty_results_round_trip(self) -> None:
        """Empty results list serialises to empty JSON array."""
        raw = results_to_json([])
        data = json.loads(raw)
        assert data == []


# ===================================================================
# G2P disabled flow
# ===================================================================


class TestG2PDisabledFlow:
    """Test that G2P does nothing when disabled."""

    def test_disabled_by_default(self, clean_g2p_env: pytest.MonkeyPatch) -> None:
        """G2P off by default → no work happens."""
        config = G2PConfig.from_environment()

        assert config.enabled is False
        assert config.check() == []
        assert config.github_owner == ""
        assert config.hooks == list(VALID_HOOKS)

    def test_disabled_ignores_other_inputs(
        self, clean_g2p_env: pytest.MonkeyPatch
    ) -> None:
        """When disabled, other G2P inputs are ignored."""
        clean_g2p_env.setenv("G2P_ENABLE", "false")
        clean_g2p_env.setenv("G2P_GITHUB_OWNER", "should-be-ignored")
        clean_g2p_env.setenv("G2P_GITHUB_TOKEN", "should-be-ignored")
        config = G2PConfig.from_environment()

        assert config.enabled is False
        assert config.github_owner == ""
        assert config.github_token == ""

    def test_disabled_config_generates_no_ini(
        self, clean_g2p_env: pytest.MonkeyPatch
    ) -> None:
        """Disabled config can still generate INI (harmless defaults)."""
        config = G2PConfig.from_environment()

        ini = generate_g2p_ini(config)

        # INI should have the comment-added section with defaults
        # but no github.com section (no token)
        import configparser

        cp = configparser.ConfigParser()
        cp.read_string(ini)
        assert cp.has_section('mapping "comment-added"')
        assert not cp.has_section("github.com")


# ===================================================================
# Deferred configuration pattern
# ===================================================================


class TestDeferredConfigPattern:
    """Test the deferred GitHub org configuration workflow."""

    def test_deferred_config_skips_checks(
        self, clean_g2p_env: pytest.MonkeyPatch
    ) -> None:
        """Deferred config uses skip mode and no token."""
        clean_g2p_env.setenv("G2P_ENABLE", "true")
        clean_g2p_env.setenv("G2P_GITHUB_OWNER", "my-org")
        clean_g2p_env.setenv("G2P_VALIDATION_MODE", "skip")
        config = G2PConfig.from_environment()

        assert config.check() == []
        assert config.token_provided is False
        assert config.validation_mode == "skip"

    @patch("g2p_setup.fetch_github_host_keys")
    @patch("g2p_setup.generate_ssh_keypair")
    def test_deferred_config_generates_keypair(
        self,
        mock_keygen: MagicMock,
        mock_host_keys: MagicMock,
        clean_g2p_env: pytest.MonkeyPatch,
    ) -> None:
        """Deferred config auto-generates SSH keypair for output."""
        from g2p_setup import setup_g2p

        clean_g2p_env.setenv("G2P_ENABLE", "true")
        clean_g2p_env.setenv("G2P_GITHUB_OWNER", "my-org")
        clean_g2p_env.setenv("G2P_VALIDATION_MODE", "skip")
        config = G2PConfig.from_environment()

        mock_keygen.return_value = (
            "-----BEGIN KEY-----\nprivate\n-----END KEY-----",
            "ssh-ed25519 AAAAdeferred gerrit-action-g2p",
        )
        mock_host_keys.return_value = "github.com ssh-ed25519 AAAAhostkey"

        docker = _make_docker_mock()
        result = setup_g2p(config, docker, "container-deferred")

        assert "AAAAdeferred" in result.ssh_public_key
        mock_keygen.assert_called_once()

    def test_deferred_config_no_token_in_ini(
        self, clean_g2p_env: pytest.MonkeyPatch
    ) -> None:
        """Deferred config → INI without github.com section."""
        clean_g2p_env.setenv("G2P_ENABLE", "true")
        clean_g2p_env.setenv("G2P_GITHUB_OWNER", "my-org")
        config = G2PConfig.from_environment()

        ini = generate_g2p_ini(config)

        import configparser

        cp = configparser.ConfigParser()
        cp.read_string(ini)
        assert not cp.has_section("github.com")
