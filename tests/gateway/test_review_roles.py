from __future__ import annotations

import subprocess

from lemoncrow.pro.capabilities.review.review_priority import QueueReason, TargetPriority
from lemoncrow.pro.capabilities.review.review_roles import infer_review_role, review_material_projection
from lemoncrow.pro.capabilities.review.targets import ReviewTarget, TargetSpan, TargetVerification


def _target(
    path: str,
    *,
    target_id: str,
    additions: int,
    deletions: int,
    attention: str = "normal",
    verification: TargetVerification | None = None,
    tier: int = 4,
) -> ReviewTarget:
    verification = verification or TargetVerification()
    return ReviewTarget(
        target_id=target_id,
        unit_key=f"unit:{target_id}",
        kind="symbol",
        path=path,
        label=target_id,
        symbol="symbol",
        start_line=1,
        end_line=max(1, additions),
        hunk_ordinals=(0,),
        spans=(TargetSpan("new", 1, max(1, additions), 0),),
        state="unreviewed",
        changed_since_mark=False,
        reviewed_revision_id="",
        attention_rank=10,
        attention_level=attention,  # type: ignore[arg-type]
        reasons=(),
        additions=additions,
        deletions=deletions,
        fingerprint_method="symbol",
        verification=verification,
        priority=TargetPriority(
            tier=tier,
            attention="routine" if tier > 4 else "understand",
            outstanding=True,
            reasons=(QueueReason("unreviewed", "Not reviewed"),),
        ),
    )


def _git(cwd, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(cwd), *args], text=True).strip()


def test_deterministic_fallback_roles_do_not_need_a_model() -> None:
    assert infer_review_role("src/auth.py") == "implementation"
    assert infer_review_role("tests/test_auth.py") == "tests"
    assert infer_review_role("docs/review.md") == "documentation"
    assert infer_review_role("locales/de/messages.po") == "localization"
    assert infer_review_role("dist/client.min.js") == "generated"
    assert infer_review_role("AGENTS.md") == "agent_guidance"
    assert infer_review_role("assets/logo.svg") == "assets"


def test_trusted_base_gitattributes_role_wins_over_fallback(tmp_path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / ".gitattributes").write_text(
        "tests/** lemoncrow-review-role=implementation\n" "vendor/** linguist-generated\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "contract.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "bundle.js").write_text("x=1;\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")

    projected = review_material_projection(
        [
            _target("tests/contract.py", target_id="t-test", additions=10, deletions=2),
            _target("vendor/bundle.js", target_id="t-vendor", additions=90, deletions=10),
        ],
        repo_root=str(tmp_path),
        base_sha=base,
    )
    by_path = {row["path"]: row for row in projected["files"]}

    assert by_path["tests/contract.py"]["role"] == "implementation"
    assert by_path["tests/contract.py"]["source"] == "gitattributes"
    assert by_path["vendor/bundle.js"]["role"] == "generated"
    assert by_path["vendor/bundle.js"]["source"] == "gitattributes"
    assert projected["implementation_changed_lines"] == 12
    assert projected["total_changed_lines"] == 112
    assert projected["explicit_metadata_state"] == "present"


def test_supporting_material_collapses_only_without_required_attention() -> None:
    projected = review_material_projection(
        [
            _target("docs/guide.md", target_id="t-doc", additions=20, deletions=0, tier=5),
            _target(
                "tests/test_contract.py",
                target_id="t-test",
                additions=5,
                deletions=1,
                attention="high",
                verification=TargetVerification(fail_count=1),
                tier=1,
            ),
            _target("src/core.py", target_id="t-core", additions=30, deletions=4),
        ],
        repo_root="",
        base_sha="",
    )
    by_path = {row["path"]: row for row in projected["files"]}

    assert by_path["docs/guide.md"]["collapsed_default"] is True
    assert by_path["tests/test_contract.py"]["collapsed_default"] is False
    assert by_path["src/core.py"]["collapsed_default"] is False
    assert projected["implementation_changed_lines"] == 34
    assert projected["total_changed_lines"] == 60
