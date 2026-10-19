"""Configuration: one endpoint, one credential, one writable directory.

These are the settings an IT team provisions and a security review reads back,
so each one is asserted for what it accepts *and* for what it refuses. A
configuration layer that silently accepts a credential in a URL, or a settings
file that can set ``PATH``, is a finding regardless of what the rest of the
package does.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from lemoncrow_client.config import (
    DEFAULT_URL,
    load_config,
    read_settings_file,
    repo_identity,
)
from lemoncrow_client.errors import ClientError, ErrorCode


def _env(state_dir: Path, **extra: str) -> dict[str, str]:
    return {"LEMONCROW_HOME": str(state_dir), "HOME": str(state_dir.parent), **extra}


def test_the_default_endpoint_is_loopback(worktree: Path, state_dir: Path) -> None:
    """ "Local" is a server bound to loopback, not a different code path."""
    config = load_config(_env(state_dir), cwd=worktree)
    assert config.url == DEFAULT_URL
    assert config.install_mode == "local"
    assert config.hosted is False


def test_a_remote_endpoint_defaults_to_hosted_for_pre_mode_installations(worktree: Path, state_dir: Path) -> None:
    config = load_config(_env(state_dir, LEMONCROW_URL="https://lemoncrow.example"), cwd=worktree)
    assert config.install_mode == "hosted"
    assert config.hosted is True


def test_explicit_hosted_mode_is_a_security_boundary_even_on_loopback(worktree: Path, state_dir: Path) -> None:
    config = load_config(
        _env(
            state_dir,
            LEMONCROW_URL="http://127.0.0.1:7420",
            LEMONCROW_INSTALL_MODE="hosted",
        ),
        cwd=worktree,
    )
    assert config.hosted is True


def test_invalid_install_mode_is_refused(worktree: Path, state_dir: Path) -> None:
    with pytest.raises(ClientError) as caught:
        load_config(_env(state_dir, LEMONCROW_INSTALL_MODE="hybrid"), cwd=worktree)
    assert caught.value.code is ErrorCode.NOT_CONFIGURED


@pytest.mark.parametrize(
    "url",
    ["ftp://host/x", "file:///etc/passwd", "not-a-url", "https://", "gopher://host"],
)
def test_a_non_http_endpoint_is_refused(worktree: Path, state_dir: Path, url: str) -> None:
    with pytest.raises(ClientError) as caught:
        load_config(_env(state_dir, LEMONCROW_URL=url), cwd=worktree)
    assert caught.value.code is ErrorCode.NOT_CONFIGURED


def test_an_endpoint_with_embedded_credentials_is_refused(worktree: Path, state_dir: Path) -> None:
    """A credential in the endpoint ends up in every log line that names it."""
    with pytest.raises(ClientError) as caught:
        load_config(_env(state_dir, LEMONCROW_URL="https://user:secret@host/"), cwd=worktree)
    assert "credentials" in caught.value.message


def test_the_origin_is_derived_once_and_defaults_the_port_by_scheme(worktree: Path, state_dir: Path) -> None:
    https = load_config(_env(state_dir, LEMONCROW_URL="https://lemoncrow.corp"), cwd=worktree)
    assert https.endpoint_origin == ("https", "lemoncrow.corp", 443)
    http = load_config(_env(state_dir, LEMONCROW_URL="http://127.0.0.1"), cwd=worktree)
    assert http.endpoint_origin == ("http", "127.0.0.1", 80)


@pytest.mark.parametrize("url", ["http://lemoncrow.corp", "http://10.0.0.8:7420", "http://127.0.0.2:7420"])
def test_plaintext_http_is_refused_outside_literal_loopback(worktree: Path, state_dir: Path, url: str) -> None:
    with pytest.raises(ClientError) as caught:
        load_config(_env(state_dir, LEMONCROW_URL=url), cwd=worktree)
    assert caught.value.code is ErrorCode.NOT_CONFIGURED
    assert "HTTPS" in caught.value.message


@pytest.mark.parametrize(
    "url",
    ["http://localhost:7420", "http://127.0.0.1:7420", "http://[::1]:7420"],
)
def test_plaintext_http_is_allowed_only_for_literal_loopback(worktree: Path, state_dir: Path, url: str) -> None:
    assert load_config(_env(state_dir, LEMONCROW_URL=url), cwd=worktree).url == url


def test_a_short_token_is_refused_rather_than_sent(worktree: Path, state_dir: Path) -> None:
    with pytest.raises(ClientError) as caught:
        load_config(_env(state_dir, LEMONCROW_INSTALL_MODE="hosted", LEMONCROW_TOKEN="short"), cwd=worktree)
    assert caught.value.action.value == "reauthenticate"


def test_local_mode_accepts_but_does_not_require_legacy_credentials(worktree: Path, state_dir: Path) -> None:
    (state_dir / "token").write_text("legacy-local-token-0123456789", encoding="utf-8")
    os.chmod(state_dir / "token", 0o600)
    config = load_config(
        _env(state_dir, LEMONCROW_INSTALL_MODE="local", LEMONCROW_TOKEN="another-legacy-token-0123456789"),
        cwd=worktree,
    )
    assert config.authenticated is True
    assert config.token == "another-legacy-token-0123456789"
    assert config.token_source == "LEMONCROW_TOKEN"


def test_an_absent_credential_is_not_a_misconfiguration(worktree: Path, state_dir: Path) -> None:
    """The hook must still be able to report one line and continue."""
    config = load_config(_env(state_dir), cwd=worktree)
    assert config.authenticated is False
    assert config.token_source == ""


def test_the_settings_file_is_read_but_the_environment_wins(worktree: Path, state_dir: Path) -> None:
    (state_dir / "env").write_text("LEMONCROW_URL=https://from-file:1234\n", encoding="utf-8")
    assert load_config(_env(state_dir), cwd=worktree).url == "https://from-file:1234"
    overridden = load_config(_env(state_dir, LEMONCROW_URL="https://from-env:9999"), cwd=worktree)
    assert overridden.url == "https://from-env:9999"


def test_the_settings_file_cannot_set_anything_outside_this_products_prefix(
    state_dir: Path,
) -> None:
    """A settings file that could set PATH would be an escalation primitive."""
    (state_dir / "env").write_text(
        "\n".join(
            [
                "PATH=/tmp/evil",
                "LD_PRELOAD=/tmp/evil.so",
                "# a comment",
                "LEMONCROW_URL=http://ok:1",
                "LEMONCROW_HOME=/tmp/elsewhere",
                "malformed line",
            ]
        ),
        encoding="utf-8",
    )
    assert read_settings_file(state_dir) == {"LEMONCROW_URL": "http://ok:1"}


def test_an_oversized_settings_file_is_ignored(state_dir: Path) -> None:
    (state_dir / "env").write_text("LEMONCROW_URL=http://x:1\n" + "#" * 20000, encoding="utf-8")
    assert read_settings_file(state_dir) == {}


def test_the_worktree_is_discovered_by_the_git_marker(tmp_path: Path, state_dir: Path) -> None:
    root = tmp_path / "repo"
    (root / "a" / "b").mkdir(parents=True)
    (root / ".git").mkdir()
    config = load_config(_env(state_dir), cwd=root / "a" / "b")
    assert config.repo_root == root.resolve()


def test_a_directory_with_no_git_marker_is_its_own_root(tmp_path: Path, state_dir: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert load_config(_env(state_dir), cwd=plain).repo_root == plain.resolve()


def test_a_canonical_scm_identity_is_used_when_provisioned(worktree: Path) -> None:
    claim = repo_identity(worktree, {"LEMONCROW_SCM_PROVIDER": "github", "LEMONCROW_SCM_REPO_ID": "12345"})
    wire = claim.to_wire()
    assert wire["scm_provider"] == "github"
    assert wire["scm_repo_id"] == "12345"
    assert "remote_url" not in wire and "origin_url" not in wire


def test_a_repository_without_scm_identity_gets_a_stable_opaque_fingerprint(
    worktree: Path,
) -> None:
    first = repo_identity(worktree, {}).fingerprint
    assert first and first == repo_identity(worktree, {}).fingerprint
    assert "/" not in first and ":" not in first


def test_canonical_scm_identity_does_not_create_unborn_marker(tmp_path: Path) -> None:
    repo = tmp_path / "brand-new-scm"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)

    claim = repo_identity(
        repo,
        {"LEMONCROW_SCM_PROVIDER": "github", "LEMONCROW_SCM_REPO_ID": "12345"},
    )

    assert claim.scm_provider == "github"
    assert claim.scm_repo_id == "12345"
    assert not (repo / ".git" / "lemoncrow-unborn-repo-id").exists()


def test_unborn_repository_identity_survives_first_commit(tmp_path: Path) -> None:
    repo = tmp_path / "brand-new"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.test"], check=True)

    before = repo_identity(repo, {}).fingerprint
    marker = repo / ".git" / "lemoncrow-unborn-repo-id"
    assert marker.read_text(encoding="utf-8").strip() == before

    (repo / "first.txt").write_text("first\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "first.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "first"], check=True)

    assert repo_identity(repo, {}).fingerprint == before


def test_existing_committed_repository_does_not_get_unborn_identity_marker(tmp_path: Path) -> None:
    repo = tmp_path / "committed"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.test"], check=True)
    (repo / "first.txt").write_text("first\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "first.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "first"], check=True)

    identity = repo_identity(repo, {}).fingerprint
    root = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--max-parents=0", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.splitlines()[0]
    assert identity == root
    assert not (repo / ".git" / "lemoncrow-unborn-repo-id").exists()


def test_an_unborn_orphan_branch_never_moves_a_committed_repository_identity(tmp_path: Path) -> None:
    repo = tmp_path / "orphaned"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Fixture"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.test"], check=True)
    (repo / "first.txt").write_text("first\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "first.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "first"], check=True)
    identity = repo_identity(repo, {}).fingerprint

    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--orphan", "pages"], check=True)
    repo_identity(repo, {})
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-f", "main"], check=True)

    assert repo_identity(repo, {}).fingerprint == identity
    assert not (repo / ".git" / "lemoncrow-unborn-repo-id").exists()


def test_the_state_directory_is_the_only_permitted_location_outside_the_repo(worktree: Path, state_dir: Path) -> None:
    config = load_config(_env(state_dir), cwd=worktree)
    assert config.state_dir == state_dir
    assert config.repo_root == worktree.resolve()


def test_a_group_readable_token_file_is_refused(worktree: Path, state_dir: Path) -> None:
    target = state_dir / "token"
    target.write_text("provisioned-token-0123456789", encoding="utf-8")
    os.chmod(target, 0o640)
    with pytest.raises(ClientError) as caught:
        load_config(_env(state_dir, LEMONCROW_INSTALL_MODE="hosted"), cwd=worktree)
    assert "chmod 600" in caught.value.message


def test_an_explicit_token_file_path_is_honoured(tmp_path: Path, worktree: Path, state_dir: Path) -> None:
    target = tmp_path / "provisioned"
    target.write_text("it-provisioned-token-0123456789", encoding="utf-8")
    os.chmod(target, 0o600)
    config = load_config(
        _env(state_dir, LEMONCROW_INSTALL_MODE="hosted", LEMONCROW_TOKEN_FILE=str(target)), cwd=worktree
    )
    assert config.token == "it-provisioned-token-0123456789"
    assert config.token_source == str(target)


def test_local_fs_is_off_unless_explicitly_enabled(worktree: Path, state_dir: Path) -> None:
    assert load_config(_env(state_dir), cwd=worktree).offer_local_fs is False
    assert load_config(_env(state_dir, LEMONCROW_LOCAL_FS="1"), cwd=worktree).offer_local_fs is True


def test_resolution_cache_budget_can_be_overridden_or_disabled(worktree: Path, state_dir: Path) -> None:
    one_gib = 1024 * 1024 * 1024
    assert (
        load_config(_env(state_dir, LEMONCROW_RESOLUTION_CACHE_BYTES="1G"), cwd=worktree).resolution_cache_bytes
        == one_gib
    )
    assert load_config(_env(state_dir, LEMONCROW_RESOLUTION_CACHE_BYTES="0"), cwd=worktree).resolution_cache_bytes == 0


def test_cache_disabled_overrides_an_explicit_nonzero_cache_budget(worktree: Path, state_dir: Path) -> None:
    """``LEMONCROW_CACHE_DISABLED`` is the one global kill switch, same name the
    private server's own tool-result caches read -- it must win even over an
    explicit ``LEMONCROW_RESOLUTION_CACHE_BYTES`` asking for a real budget.
    """
    env = _env(state_dir, LEMONCROW_RESOLUTION_CACHE_BYTES="1G", LEMONCROW_CACHE_DISABLED="1")
    assert load_config(env, cwd=worktree).resolution_cache_bytes == 0


def test_cache_disabled_is_off_unless_explicitly_enabled(worktree: Path, state_dir: Path) -> None:
    assert load_config(_env(state_dir), cwd=worktree).resolution_cache_bytes > 0


def test_a_nonsense_timeout_falls_back_to_the_default_rather_than_zero(worktree: Path, state_dir: Path) -> None:
    """A zero budget would turn every request into an immediate failure."""
    config = load_config(
        _env(state_dir, LEMONCROW_STARTUP_BUDGET_S="nonsense", LEMONCROW_REQUEST_TIMEOUT_S="-4"),
        cwd=worktree,
    )
    assert config.startup_budget_s == 1.0
    assert config.request_timeout_s == 10.0


def test_product_state_roots_are_namespaced_below_lemoncrow_home(worktree: Path, state_dir: Path) -> None:
    config = load_config(_env(state_dir), cwd=worktree)

    assert config.account_state_dir == state_dir / "account"
    assert config.review_state_dir == state_dir / "review"
    assert config.runtime_state_dir == state_dir / "runtime"
    assert not config.account_state_dir.exists()
    assert not config.review_state_dir.exists()
    assert not config.runtime_state_dir.exists()


def test_review_and_runtime_settings_can_point_at_independent_endpoints(worktree: Path, state_dir: Path) -> None:
    (state_dir / "review").mkdir()
    (state_dir / "runtime").mkdir()
    (state_dir / "review" / "env").write_text(
        "LEMONCROW_URL=https://review.example\nLEMONCROW_INSTALL_MODE=hosted\n",
        encoding="utf-8",
    )
    (state_dir / "runtime" / "env").write_text(
        "LEMONCROW_URL=http://127.0.0.1:7420\nLEMONCROW_INSTALL_MODE=local\n",
        encoding="utf-8",
    )

    review = load_config(_env(state_dir), cwd=worktree, product="review")
    runtime = load_config(_env(state_dir), cwd=worktree, product="runtime")

    assert review.url == "https://review.example"
    assert review.hosted is True
    assert review.product == "review"
    assert runtime.url == "http://127.0.0.1:7420"
    assert runtime.hosted is False
    assert runtime.product == "runtime"


def test_runtime_lite_is_an_explicit_serverless_install_mode(worktree: Path, state_dir: Path) -> None:
    (state_dir / "runtime").mkdir()
    (state_dir / "runtime" / "env").write_text("LEMONCROW_INSTALL_MODE=lite\n", encoding="utf-8")

    runtime = load_config(_env(state_dir), cwd=worktree, product="runtime")

    assert runtime.install_mode == "lite"
    assert runtime.hosted is False


def test_a_pre_lite_local_runtime_install_resolves_to_runtime_lite(worktree: Path, state_dir: Path) -> None:
    # The local server no longer executes Runtime tools. A `runtime/env` that
    # pointed the thin client at it now runs Runtime Lite; Review keeps its
    # local server.
    for product in ("review", "runtime"):
        (state_dir / product).mkdir()
        (state_dir / product / "env").write_text(
            "LEMONCROW_URL=http://127.0.0.1:7420\nLEMONCROW_INSTALL_MODE=local\n", encoding="utf-8"
        )

    assert load_config(_env(state_dir), cwd=worktree, product="runtime").install_mode == "lite"
    assert load_config(_env(state_dir), cwd=worktree, product="review").install_mode == "local"


def test_runtime_without_a_hosted_endpoint_is_runtime_lite(worktree: Path, state_dir: Path) -> None:
    assert load_config(_env(state_dir), cwd=worktree, product="runtime").install_mode == "lite"
    hosted = load_config(_env(state_dir, LEMONCROW_URL="https://lemoncrow.example"), cwd=worktree, product="runtime")
    assert hosted.install_mode == "hosted"


def test_product_settings_override_legacy_shared_settings_but_environment_wins(worktree: Path, state_dir: Path) -> None:
    (state_dir / "env").write_text("LEMONCROW_URL=https://legacy.example\n", encoding="utf-8")
    (state_dir / "review").mkdir()
    (state_dir / "review" / "env").write_text("LEMONCROW_URL=https://review.example\n", encoding="utf-8")

    assert load_config(_env(state_dir), cwd=worktree, product="review").url == "https://review.example"
    assert load_config(_env(state_dir), cwd=worktree, product="runtime").url == "https://legacy.example"
    assert (
        load_config(
            _env(state_dir, LEMONCROW_URL="https://process.example"),
            cwd=worktree,
            product="review",
        ).url
        == "https://process.example"
    )


def test_unknown_product_is_refused(worktree: Path, state_dir: Path) -> None:
    with pytest.raises(ValueError, match="unknown LemonCrow product"):
        load_config(_env(state_dir), cwd=worktree, product="unknown")


def test_hosted_mode_never_sends_the_local_machine_token(worktree: Path, state_dir: Path) -> None:
    """SL-6: ~/.lemoncrow/token is the local server's credential, not a hosted login."""

    from lemoncrow_client.credentials import write_managed_credentials

    local_token = state_dir / "token"
    local_token.write_text("lc_local_" + "A" * 40, encoding="utf-8")
    os.chmod(local_token, 0o600)
    write_managed_credentials(
        state_dir / "account", access_token="managed-access-0123456789", refresh_token="managed-refresh-0123456789"
    )

    hosted = load_config(_env(state_dir, LEMONCROW_URL="https://hosted.example.com"), cwd=worktree, product="review")
    assert hosted.hosted is True
    assert hosted.token == "managed-access-0123456789"
    assert hosted.refresh_token == "managed-refresh-0123456789"

    # An explicit local machine token is still never sent to a hosted origin.
    pinned = load_config(
        _env(state_dir, LEMONCROW_URL="https://hosted.example.com", LEMONCROW_TOKEN_FILE=str(local_token)),
        cwd=worktree,
    )
    assert pinned.token == "managed-access-0123456789"

    # Local mode keeps using the machine token.
    assert load_config(_env(state_dir), cwd=worktree, product="review").token.startswith("lc_local_")


def test_hosted_mode_honours_an_explicit_operator_token_over_managed_login(worktree: Path, state_dir: Path) -> None:
    from lemoncrow_client.credentials import write_managed_credentials

    write_managed_credentials(
        state_dir / "account", access_token="managed-access-0123456789", refresh_token="managed-refresh-0123456789"
    )
    config = load_config(
        _env(state_dir, LEMONCROW_URL="https://hosted.example.com", LEMONCROW_TOKEN="operator-token-0123456789"),
        cwd=worktree,
    )
    assert config.token == "operator-token-0123456789"
    assert config.token_source == "LEMONCROW_TOKEN"
