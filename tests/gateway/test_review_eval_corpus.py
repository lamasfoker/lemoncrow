"""Review evaluation corpus: format, provenance, organization opt-in and matching rules."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from lemoncrow.pro.capabilities.review.evaluation.corpus import (
    BUILTIN_CORPUS_RELATIVE,
    CorpusError,
    ExpectedBehavior,
    ExpectedFinding,
    load_builtin_corpus,
    load_corpus,
    parse_case,
)
from lemoncrow.pro.capabilities.review.evaluation.matching import match_behavior, match_findings
from lemoncrow.pro.capabilities.review.evaluation.pipeline import ObservedBehavior, ObservedFinding

REPO = Path(__file__).resolve().parents[2]


def _case(**overrides: Any) -> dict[str, Any]:
    case: dict[str, Any] = {
        "id": "case-one",
        "title": "A token check changes",
        "kind": "defect",
        "provenance": {"source": "synthetic", "note": "authored for this test"},
        "license": "Apache-2.0",
        "base": {"src/auth.py": "def check():\n    return 1\n"},
        "head": {"src/auth.py": "def check():\n    return 2\n"},
        "expected": {
            "findings": [{"id": "f", "path": "src/auth.py", "line": 2, "category": "security", "severity": "p1"}]
        },
    }
    case.update(overrides)
    return case


def _write_corpus(root: Path, *cases: dict[str, Any], **manifest: Any) -> Path:
    (root / "cases").mkdir(parents=True)
    body = {
        "id": "test-corpus",
        "version": 1,
        "visibility": "public",
        "license": "Apache-2.0",
        "cases": [case["id"] for case in cases],
        **manifest,
    }
    (root / "corpus.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    for case in cases:
        (root / "cases" / f"{case['id']}.yaml").write_text(yaml.safe_dump(case), encoding="utf-8")
    return root


def _organization_manifest(**opt_in: Any) -> dict[str, Any]:
    record = {"organization": "acme", "approved_by": "owner@acme", "approved_at": "2026-10-01", "reference": "DPA-1"}
    record.update(opt_in)
    return {"visibility": "organization", "organization": "acme", "opt_in": record}


def test_builtin_corpus_is_public_with_provenance_and_license_per_case() -> None:
    corpus = load_builtin_corpus(REPO)

    assert corpus.visibility == "public"
    assert len(corpus.cases) >= 10
    assert {case.kind for case in corpus.cases} == {"defect", "clean"}
    assert any(case.expected_findings for case in corpus.cases)
    assert any(case.expected_behavior for case in corpus.cases)
    for case in corpus.cases:
        assert case.provenance.source in {"synthetic", "public_repository"}, case.id
        assert case.provenance.note, case.id
        assert case.license == "Apache-2.0", case.id


def test_case_validation_reports_every_problem() -> None:
    raw = _case(
        kind="clean",
        provenance=None,
        head={"../escape.py": "x", ".lemoncrow/review.yaml": "version: 1\n"},
    )

    case, problems = parse_case(raw, where="cases/case-one.yaml", visibility="public")

    assert case is None
    text = "\n".join(problems)
    assert "provenance is required" in text
    assert "must be a relative path inside the repository" in text
    assert "review_config" in text
    assert "a clean case cannot expect findings" in text


def test_public_corpus_refuses_private_or_unlicensed_cases(tmp_path: Path) -> None:
    for case_index, (provenance, license_id, message) in enumerate(
        [
            ({"source": "organization_review", "note": "customer"}, "Apache-2.0", "not allowed in a public corpus"),
            ({"source": "internal_repository", "note": "ours", "repository": "r", "commit": "c"}, "MIT", "not allowed"),
            ({"source": "public_repository", "note": "oss", "repository": "r", "commit": "c"}, "GPL-3.0", "permissive"),
            ({"source": "public_repository", "note": "oss"}, "MIT", "needs repository and commit"),
        ]
    ):
        case_tmp_path = tmp_path / f"case-{case_index}"
        case_tmp_path.mkdir()
        root = _write_corpus(case_tmp_path / "corpus", _case(provenance=provenance, license=license_id))
        with pytest.raises(CorpusError) as caught:
            load_corpus(root)
        assert any(message in problem for problem in caught.value.problems)


def test_organization_corpus_needs_explicit_matching_opt_in(tmp_path: Path) -> None:
    customer = _case(provenance={"source": "organization_review", "note": "Review 42"}, license="proprietary")
    root = _write_corpus(tmp_path / "acme", customer, **_organization_manifest())

    with pytest.raises(CorpusError, match="requires an explicit organization opt-in"):
        load_corpus(root, repository_root=REPO)
    with pytest.raises(CorpusError, match="belongs to 'acme'"):
        load_corpus(root, organization_opt_in="globex", repository_root=REPO)

    corpus = load_corpus(root, organization_opt_in="acme", repository_root=REPO)

    assert corpus.visibility == "organization"
    assert corpus.opt_in is not None and corpus.opt_in.reference == "DPA-1"


def test_organization_corpus_needs_an_opt_in_record(tmp_path: Path) -> None:
    manifest = _organization_manifest()
    manifest.pop("opt_in")
    root = _write_corpus(tmp_path / "acme", _case(), **manifest)

    with pytest.raises(CorpusError) as caught:
        load_corpus(root, organization_opt_in="acme", repository_root=REPO)

    assert any("opt_in record" in problem for problem in caught.value.problems)


def test_organization_corpus_is_never_bundled(tmp_path: Path) -> None:
    repository = tmp_path / "checkout"
    inside = _write_corpus(repository / "evals" / "acme", _case(), **_organization_manifest())

    with pytest.raises(CorpusError, match="never bundled"):
        load_corpus(inside, organization_opt_in="acme", repository_root=repository)

    builtin = _write_corpus(repository / BUILTIN_CORPUS_RELATIVE, _case(), **_organization_manifest())
    with pytest.raises(CorpusError, match="expected a public corpus"):
        load_builtin_corpus(repository)
    assert builtin.is_dir()


def test_every_corpus_checked_into_the_repository_is_public() -> None:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*corpus.yaml"],
        cwd=REPO,
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")
    manifests = [REPO / name.decode() for name in listed if name]

    assert manifests, "the built-in corpus manifest must be tracked"
    for manifest in manifests:
        raw = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "visibility" in raw:
            assert raw["visibility"] == "public", f"{manifest} must never be committed"


def _finding(ref: str, path: str, start: int, end: int, category: str, *, surfaced: bool = True) -> ObservedFinding:
    return ObservedFinding(
        ref=ref,
        source="ai_review",
        entry="ai.investigators",
        path=path,
        start_line=start,
        end_line=end,
        category=category,
        severity="p1",
        title=ref,
        surfaced=surfaced,
    )


def test_finding_matching_uses_path_category_and_line_tolerance() -> None:
    expected = [
        ExpectedFinding("near", "a.py", 10, 10, ("security",)),
        ExpectedFinding("far", "a.py", 40, 40, ("security",)),
        ExpectedFinding("category", "b.py", 5, 5, ("performance",)),
    ]
    observed = [
        _finding("ai:1", "a.py", 13, 15, "security"),  # 3 lines away: within the default tolerance
        _finding("ai:2", "a.py", 44, 44, "security"),  # 4 lines away: outside
        _finding("ai:3", "b.py", 5, 5, "correctness"),  # wrong category
        _finding("ai:4", "a.py", 40, 40, "security", surfaced=False),  # folded: neither catch nor noise
    ]

    matches, missed, unmatched = match_findings(expected, observed, tolerance=3)

    assert [(item.expected.id, item.finding.ref, item.line_gap) for item in matches] == [("near", "ai:1", 3)]
    assert [item.id for item in missed] == ["far", "category"]
    assert [item.ref for item in unmatched] == ["ai:2", "ai:3"]


def test_finding_matching_is_one_to_one_and_maximum() -> None:
    expected = [
        ExpectedFinding("wide", "a.py", 1, 20, ("security",)),
        ExpectedFinding("narrow", "a.py", 2, 2, ("security",)),
    ]
    observed = [_finding("ai:a", "a.py", 2, 2, "security"), _finding("ai:b", "a.py", 18, 18, "security")]

    matches, missed, unmatched = match_findings(expected, observed, tolerance=0)

    assert sorted((item.expected.id, item.finding.ref) for item in matches) == [("narrow", "ai:a"), ("wide", "ai:b")]
    assert missed == [] and unmatched == []


def test_behavior_matching_uses_kind_subject_pattern_and_change() -> None:
    def delta(subject: str, change: str) -> ObservedBehavior:
        return ObservedBehavior(
            ref=f"beh:{subject}",
            kind="side_effect",
            subject=subject,
            change=change,
            severity="p1",
            critical=False,
            paths=(),
            entry="side_effects.static",
        )

    expected = [
        ExpectedBehavior("env", "side_effect", "env_var:AUTH_*", "added"),
        ExpectedBehavior("host", "side_effect", "outbound_host:*", "removed"),
    ]
    observed = [delta("env_var:AUTH_BYPASS", "added"), delta("outbound_host:api.acme.net", "added")]

    matches, missed, unexpected = match_behavior(expected, observed)

    assert [(item.expected.id, item.observed.subject) for item in matches] == [("env", "env_var:AUTH_BYPASS")]
    assert [item.id for item in missed] == ["host"]
    assert [item.subject for item in unexpected] == ["outbound_host:api.acme.net"]
