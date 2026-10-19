"""`lc rt savings` consent, undo and host-parity surfaces (G4-VLW)."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from lemoncrow.core.capabilities.verified_work.models import Diagnostic, VerifiedWorkOpportunity
from lemoncrow.gateway.cli import cli
from lemoncrow.pro.capabilities.verified_work.consent import (
    grant_auto,
    load_grant,
    local_actor,
    repository_identity,
)
from lemoncrow.pro.capabilities.verified_work.product import (
    IsolationStatus,
    KindQualification,
    RepositoryStatus,
    SavingsProductStatus,
)
from lemoncrow.pro.capabilities.verified_work.promote import promote_candidate
from lemoncrow.pro.capabilities.verified_work.repairs import RepairJournal, RepairMeta

MODEL = "custom/ollama/test-coder"


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "a.txt").write_text("bad\n", encoding="utf-8")
    for args in (("init",), ("add", "."), ("-c", "user.email=t@e", "-c", "user.name=t", "commit", "-m", "b")):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    return path


def _status(
    grantable: tuple[tuple[str, str], ...], *, state: str = "ready", key: str = "k", mode: str = "shadow"
) -> SavingsProductStatus:
    return SavingsProductStatus(
        state,  # type: ignore[arg-type]
        mode,
        MODEL,
        "probed_local_model",
        IsolationStatus(True, True, True, "qualified"),
        (KindQualification("lint", 20, 1.0, 100.0, True, True, 20, 0.1),),
        True,
        20,
        0.1,
        "ready_for_auto",
        repository=RepositoryStatus(key=key, root="/repo", grant=None, windows=(), grantable=grantable, auto_pairs=()),
    )


def _mock_status(monkeypatch, grantable: tuple[tuple[str, str], ...]) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.pro.capabilities.verified_work import product

    monkeypatch.setattr(
        product, "product_status", lambda root, settings, repo=None: _status(grantable, mode=settings.mode)
    )


def _invoke(root: Path, *args: str):  # type: ignore[no-untyped-def]
    return CliRunner().invoke(cli, ["--root", str(root), "runtime", "savings", *args])


def test_auto_opts_in_the_repository_with_who_and_when(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "lc"
    repo = _repo(tmp_path / "repo")
    _mock_status(monkeypatch, ((MODEL, "lint"),))

    result = _invoke(root, "auto", "--repo", str(repo), "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["grant"]["granted_by"] == local_actor()
    assert payload["grant"]["granted_at"]
    assert payload["grant"]["pairs"] == [[MODEL, "lint"]]
    identity = repository_identity(repo)
    assert identity is not None
    assert load_grant(identity.key, root=root) is not None
    stored = json.loads((root / "plugin_settings.json").read_text())
    assert stored["verified_work.mode"] == "enforce"


def test_auto_refuses_until_the_repository_is_ready(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "lc"
    repo = _repo(tmp_path / "repo")
    _mock_status(monkeypatch, ())

    result = _invoke(root, "auto", "--repo", str(repo))

    assert result.exit_code != 0
    assert "not Ready yet" in result.output
    identity = repository_identity(repo)
    assert identity is not None
    assert load_grant(identity.key, root=root) is None
    settings_file = root / "plugin_settings.json"
    assert not settings_file.exists() or json.loads(settings_file.read_text()).get("verified_work.mode") != "enforce"


def test_auto_requires_a_repository(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    plain = tmp_path / "plain"
    plain.mkdir()
    _mock_status(monkeypatch, ((MODEL, "lint"),))

    result = _invoke(tmp_path / "lc", "auto", "--repo", str(plain))

    assert result.exit_code != 0
    assert "repository" in result.output


def test_auto_revoke_withdraws_the_repository_opt_in(tmp_path: Path) -> None:
    root = tmp_path / "lc"
    repo = _repo(tmp_path / "repo")
    identity = repository_identity(repo)
    assert identity is not None
    grant_auto(identity, [(MODEL, "lint")], root=root)

    result = _invoke(root, "auto", "--revoke", "--repo", str(repo))

    assert result.exit_code == 0, result.output
    assert "revoked" in result.output.lower()
    assert load_grant(identity.key, root=root) is None


def test_learn_and_disable_revoke_every_opt_in(tmp_path: Path) -> None:
    root = tmp_path / "lc"
    identity = repository_identity(_repo(tmp_path / "repo"))
    assert identity is not None

    grant_auto(identity, [(MODEL, "lint")], root=root)
    learn = _invoke(root, "learn")
    assert learn.exit_code == 0, learn.output
    assert load_grant(identity.key, root=root) is None
    assert json.loads((root / "plugin_settings.json").read_text())["verified_work.mode"] == "shadow"

    grant_auto(identity, [(MODEL, "lint")], root=root)
    disable = _invoke(root, "disable")
    assert disable.exit_code == 0, disable.output
    assert load_grant(identity.key, root=root) is None
    assert json.loads((root / "plugin_settings.json").read_text())["verified_work.mode"] == "off"


def test_settings_enforce_alone_creates_no_opt_in(tmp_path: Path) -> None:
    root = tmp_path / "lc"
    repo = _repo(tmp_path / "repo")

    result = CliRunner().invoke(cli, ["--root", str(root), "settings", "set", "verified_work.mode", "enforce"])

    assert result.exit_code == 0, result.output
    identity = repository_identity(repo)
    assert identity is not None
    assert load_grant(identity.key, root=root) is None


def _promoted_repair(repo: Path, root: Path) -> str:
    identity = repository_identity(repo)
    assert identity is not None
    candidate = repo.parent / "candidate"
    candidate.mkdir()
    (candidate / "a.txt").write_text("good\n", encoding="utf-8")
    opportunity = VerifiedWorkOpportunity(
        id="x",
        session_id="s",
        workspace=str(repo),
        command="ruff check a.txt",
        cwd=str(repo),
        kind="lint",
        exit_code=1,
        diagnostics=(Diagnostic("a.txt", 1, 1, "E", "bad"),),
        diagnostic_digest="d",
        diagnostic_excerpt="bad",
        implicated_paths=("a.txt",),
        base_file_hashes=(("a.txt", hashlib.sha256((repo / "a.txt").read_bytes()).hexdigest()),),
        original_result_digest="r",
    )
    meta = RepairMeta(
        workspace=str(repo),
        repo_key=identity.key,
        repo_root=identity.root,
        session_id="s",
        host="claude",
        model=MODEL,
        kind="lint",
        verifier_command="ruff check a.txt",
    )
    result = promote_candidate(
        opportunity,
        candidate_workspace=candidate,
        changed_paths=("a.txt",),
        journal=RepairJournal(meta, root=root),
    )
    assert result.promoted is True
    return result.record_id


def test_undo_last_restores_and_names_files(tmp_path: Path) -> None:
    root = tmp_path / "lc"
    repo = _repo(tmp_path / "repo")
    record_id = _promoted_repair(repo, root)

    result = _invoke(root, "undo", "--last", "--repo", str(repo))

    assert result.exit_code == 0, result.output
    assert record_id in result.output
    assert "a.txt" in result.output
    assert (repo / "a.txt").read_text(encoding="utf-8") == "bad\n"


def test_undo_by_id_refuses_on_drift_and_names_conflicting_paths(tmp_path: Path) -> None:
    root = tmp_path / "lc"
    repo = _repo(tmp_path / "repo")
    record_id = _promoted_repair(repo, root)
    (repo / "a.txt").write_text("edited after repair\n", encoding="utf-8")

    result = _invoke(root, "undo", record_id)

    assert result.exit_code != 0
    assert "a.txt" in result.output
    assert "changed after" in result.output
    assert (repo / "a.txt").read_text(encoding="utf-8") == "edited after repair\n"


def test_undo_requires_exactly_one_target(tmp_path: Path) -> None:
    root = tmp_path / "lc"
    for args in (("undo",), ("undo", "abc", "--last")):
        result = _invoke(root, *args)
        assert result.exit_code != 0
        assert "exactly one" in result.output


def test_status_states_host_parity_and_pending_opt_in(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "lc"
    repo = _repo(tmp_path / "repo")
    _mock_status(monkeypatch, ((MODEL, "lint"),))
    monkeypatch.setenv("LEMONCROW_VERIFIED_WORK_MODE", "enforce")

    text = _invoke(root, "status", "--repo", str(repo))
    raw = _invoke(root, "status", "--repo", str(repo), "--json")

    assert text.exit_code == 0, text.output
    assert "Codex" in text.output and "OpenCode" in text.output
    assert "unsupported" in text.output
    assert "lc rt savings auto" in text.output
    payload = json.loads(raw.stdout)
    assert payload["hosts"]["codex"] == "unsupported"
    assert payload["hosts"]["opencode"] == "unsupported"
    assert payload["hosts"]["claude"] == "supported"
    assert payload["undo_bounds"] == {"max_records": 50, "max_age_days": 30, "max_record_bytes": 4 * 1024 * 1024}


@pytest.fixture(autouse=True)
def _restore_mode_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # `_persist` mirrors written settings into os.environ; register the key so
    # monkeypatch restores it after each test.
    monkeypatch.setenv("LEMONCROW_VERIFIED_WORK_MODE", "off")


def _ready_repository(tmp_path: Path, monkeypatch, *, isolated: bool):  # type: ignore[no-untyped-def]
    """Real product status over a repository whose lint and typecheck windows qualify."""
    from lemoncrow.core.capabilities.verified_work.settings import VerifiedWorkSettings
    from lemoncrow.gateway.cli.commands import local_savings
    from lemoncrow.pro.capabilities.model_setup.endpoints import add_endpoint
    from lemoncrow.pro.capabilities.verified_work.outcomes import record_model_outcome

    root = tmp_path / "lc"
    model = add_endpoint(
        root, "http://127.0.0.1:11434/v1", name="ollama", models=["test-coder"], locus="local"
    ).model_ids()[0]
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.verified_work.product.probe_isolation",
        lambda: IsolationStatus(isolated, isolated, True, "qualified" if isolated else "missing network isolation"),
    )
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.verified_work.calibration.summarize_calibration",
        lambda root=None, min_pairs=20: {
            "status": "calibrating",
            "by_model_kind": [
                {"model": model, "kind": kind, "pairs": 3, "qualified": True, "median_net_savings_usd": 0.2}
                for kind in ("lint", "typecheck")
            ],
        },
    )
    settings = VerifiedWorkSettings(mode="shadow", model="auto", kinds=("lint", "typecheck"), auto_min_samples=3)
    monkeypatch.setattr(local_savings, "_settings", lambda: settings)
    repo = _repo(tmp_path / "repo")
    identity = repository_identity(repo)
    assert identity is not None
    for kind in ("lint", "typecheck"):
        for _ in range(3):
            record_model_outcome(
                model,
                kind,  # type: ignore[arg-type]
                verified=True,
                promoted=False,
                latency_ms=100,
                input_tokens=1,
                output_tokens=1,
                provider_cost_usd=0.0,
                mode="shadow",
                root=root,
                repo=identity.key,
            )
    return root, model, repo, identity


@pytest.mark.parametrize("isolated", [False, True])
def test_auto_needs_qualified_isolation_to_opt_in(tmp_path: Path, monkeypatch, isolated: bool) -> None:  # type: ignore[no-untyped-def]
    root, model, repo, identity = _ready_repository(tmp_path, monkeypatch, isolated=isolated)

    result = _invoke(root, "auto", "--repo", str(repo))

    grant = load_grant(identity.key, root=root)
    if isolated:
        assert result.exit_code == 0, result.output
        assert grant is not None
        assert grant.pairs == ((model, "lint"), (model, "typecheck"))
    else:
        assert result.exit_code != 0
        assert "not Ready yet" in result.output
        assert grant is None


def test_status_names_pairs_ready_after_the_opt_in(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    root, model, repo, identity = _ready_repository(tmp_path, monkeypatch, isolated=True)
    grant_auto(identity, [(model, "lint")], root=root)

    result = _invoke(root, "status", "--repo", str(repo))

    assert result.exit_code == 0, result.output
    assert f"Ready, not opted in: typecheck via {model}" in result.output


def test_enable_auto_in_a_repository_that_is_not_ready_keeps_learning(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.pro.capabilities.verified_work import product

    root = tmp_path / "lc"
    repo = _repo(tmp_path / "repo")
    monkeypatch.setattr(product, "ensure_local_worker", lambda root, requested="auto": (MODEL, "probed_local_model"))
    _mock_status(monkeypatch, ())

    result = _invoke(root, "enable", "--auto", "--repo", str(repo))

    assert result.exit_code == 0, result.output
    assert "staying in Learning mode" in result.output
    identity = repository_identity(repo)
    assert identity is not None
    assert load_grant(identity.key, root=root) is None
    assert json.loads((root / "plugin_settings.json").read_text())["verified_work.mode"] == "shadow"


@pytest.mark.parametrize(
    ("reason", "paths", "expected"),
    [
        ("restore_failed", (), "every file was put back"),
        ("restore_partial", ("a.txt",), "stopped part-way"),
    ],
)
def test_undo_reports_the_state_a_failed_restore_left(tmp_path: Path, monkeypatch, reason, paths, expected) -> None:  # type: ignore[no-untyped-def]
    from lemoncrow.pro.capabilities.verified_work import repairs
    from lemoncrow.pro.capabilities.verified_work.repairs import UndoResult

    monkeypatch.setattr(
        repairs, "undo_repair", lambda *args, **kwargs: UndoResult(False, reason, "abc123def456", paths)
    )

    result = _invoke(tmp_path / "lc", "undo", "abc123def456")

    assert result.exit_code != 0
    assert expected in result.output
    for path in paths:
        assert path in result.output
