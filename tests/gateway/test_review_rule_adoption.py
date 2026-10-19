"""Adopting a rule suggestion is a reviewable patch to the trusted base, never a policy change (RC12-E).

A suggestion is inert. Adoption produces the patch a human lands on the trusted
base through the repository's own review; until then the effective policy is
exactly what it was.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from lemoncrow.pro.capabilities.review.rule_suggestions import (
    RuleAdoptionError,
    RuleSuggestion,
    adoption_stable_key,
    propose_rule_adoption,
    repeated_human_feedback_suggestions,
    replace_content,
)
from lemoncrow.pro.capabilities.review.rules import ReviewGovernance, resolve_review_governance
from lemoncrow.pro.capabilities.review.session_models import Annotation, AnnotationAnchor
from lemoncrow.pro.capabilities.review.trusted_base import TrustedBaseDocument

CONFIG_PATH = ".lemoncrow/review.yaml"
STATEMENT = "Please validate tenant ownership before returning this record."


def _suggestion(**changes: object) -> RuleSuggestion:
    base = RuleSuggestion(
        id="rsg-0123456789abcdef0123456789abcdef",
        repository_scope="repo",
        candidate_statement=STATEMENT,
        path_scope=("src/api/users.py",),
        supporting_feedback_refs=("annotation:a1", "annotation:a2"),
    )
    return replace(base, **changes) if changes else base  # type: ignore[arg-type]


def _present(content: str) -> TrustedBaseDocument:
    return TrustedBaseDocument(path=CONFIG_PATH, content=content, state="present", source=f"base {CONFIG_PATH}")


ABSENT = TrustedBaseDocument(path=CONFIG_PATH, content="", state="absent")
PROVISIONAL = TrustedBaseDocument(path=CONFIG_PATH, content="", state="provisional")

EXISTING = """# Review policy for this repository.
version: 1

rules:
  # keep auth on every handler
  - stable_key: api-auth
    instruction: API handlers must enforce authentication.
    paths: [src/api/**]
  - stable_key: no-print
    instruction: Do not print in library code.

ai_content_access:
  - mode: customer_local_only
    paths: [secrets/**]
"""


def _governance(document: TrustedBaseDocument) -> ReviewGovernance:
    absent = TrustedBaseDocument(path="", content="", state="absent")
    return resolve_review_governance(document, read_base_document=lambda _path: absent)


def _keys(governance: ReviewGovernance) -> list[str]:
    """Rule keys, sorted (a resolved policy orders rules by digest, not by file position)."""

    return sorted(row.stable_key for row in governance.rules)


def _rows(governance: ReviewGovernance) -> list[tuple[str, str, tuple[str, ...]]]:
    return sorted((row.stable_key, row.instruction, row.path_scope) for row in governance.rules)


def _apply(tmp_path: Path, base: str | None, patch: str) -> str:
    """What `git apply` makes of *patch* over *base* in a fresh repository (the human's landing step)."""

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    target = repo / CONFIG_PATH
    if base is not None:
        target.parent.mkdir(parents=True)
        target.write_text(base, encoding="utf-8")
    (repo / "p.diff").write_text(patch, encoding="utf-8")
    subprocess.run(["git", "apply", "--check", "p.diff"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "apply", "p.diff"], cwd=repo, check=True, capture_output=True)
    return target.read_text(encoding="utf-8")


def test_adoption_is_a_patch_that_adds_exactly_one_rule_and_nothing_is_applied(tmp_path: Path) -> None:
    suggestion = _suggestion()
    base = _present(EXISTING)
    before = _governance(base)

    proposal = propose_rule_adoption(suggestion, base)

    # Nothing became policy: the base is untouched, the suggestion is still inert.
    assert _governance(base) == before
    assert base.content == EXISTING
    assert suggestion.to_wire()["active"] is False and suggestion.state == "suggested"
    wire = proposal.to_wire()
    assert wire["effective"] is False and wire["state"] == "proposed"
    assert wire["target_path"] == CONFIG_PATH and wire["change"] == "changed"
    assert wire["supporting_feedback_refs"] == ["annotation:a1", "annotation:a2"]
    key = adoption_stable_key(suggestion.id)
    assert proposal.stable_key == key == "learned-0123456789abcdef"

    # The patch lands cleanly with git, keeps comments and order, and adds one row.
    landed = _apply(tmp_path, EXISTING, proposal.patch)
    assert landed == proposal.proposed_content
    assert landed.startswith("# Review policy for this repository.\nversion: 1\n")
    assert "  # keep auth on every handler\n" in landed and "ai_content_access:" in landed
    assert landed.count("stable_key") == 3
    assert [
        line
        for line in proposal.patch.splitlines()
        if line.startswith(("-", "+")) and not line.startswith(("---", "+++"))
    ] == [
        line for line in proposal.patch.splitlines() if line.startswith("+") and not line.startswith("+++")
    ], "the patch only adds lines"


def test_effective_policy_changes_only_when_the_patch_lands_on_the_trusted_base(tmp_path: Path) -> None:
    suggestion = _suggestion()
    base = _present(EXISTING)
    proposal = propose_rule_adoption(suggestion, base)
    key = proposal.stable_key

    # Proposed, not landed: the trusted base's governance has no such rule, for any path.
    assert key not in _keys(_governance(base))
    assert not any(row.stable_key == key for row in _governance(base).rules_for_path("src/api/users.py"))
    # Head content is never a fallback: a proposal text is not a trusted-base document until it lands.
    assert key not in _keys(_governance(base))

    # Landed on the trusted base (what a merge of the patch makes the base): now it governs.
    landed = replace_content(base, _apply(tmp_path, EXISTING, proposal.patch))
    governance = _governance(landed)
    assert _keys(governance) == sorted(["api-auth", "no-print", key])
    adopted = next(row for row in governance.rules if row.stable_key == key)
    assert adopted.instruction == STATEMENT and adopted.path_scope == ("src/api/users.py",)
    assert adopted.trust_class == "trusted_base_repository" and adopted.authoritative
    assert adopted in governance.rules_for_path("src/api/users.py")
    assert adopted not in governance.rules_for_path("src/other.py")
    # Everything else is exactly as it was.
    assert [row for row in _rows(governance) if row[0] != key] == _rows(_governance(base))
    assert [(row.mode, row.path_scope) for row in governance.content_access] == [
        (row.mode, row.path_scope) for row in _governance(base).content_access
    ]


def test_a_missing_policy_file_is_proposed_as_a_new_file(tmp_path: Path) -> None:
    proposal = propose_rule_adoption(_suggestion(), ABSENT)
    assert proposal.change == "added" and proposal.patch.startswith(
        f"diff --git a/{CONFIG_PATH} b/{CONFIG_PATH}\nnew file"
    )
    landed = _apply(tmp_path, None, proposal.patch)
    assert landed == proposal.proposed_content
    assert _keys(_governance(replace_content(ABSENT, landed))) == [proposal.stable_key]
    assert _keys(_governance(ABSENT)) == []


@pytest.mark.parametrize(
    "base",
    [
        "version: 1\n",
        "version: 1\nrules:\n- stable_key: flush-left\n  instruction: Sequence items at the key's column.\n",
        "version: 1\nrules:   # trailing comment\n    - stable_key: wide\n      instruction: Four-space indent.\n\n# footer\n",
        "rules:\n  - stable_key: a\n    instruction: One.\nversion: 1\n",
        "\n\n",
    ],
)
def test_the_patch_matches_the_file_style_and_applies(tmp_path: Path, base: str) -> None:
    document = _present(base) if base.strip() else ABSENT
    proposal = propose_rule_adoption(_suggestion(), document)
    landed = _apply(tmp_path, base if base.strip() else None, proposal.patch)
    assert landed == proposal.proposed_content
    before = _keys(_governance(document))
    assert _keys(_governance(replace_content(document, landed))) == sorted([*before, proposal.stable_key])


@pytest.mark.parametrize(
    ("base", "why"),
    [
        (PROVISIONAL, "cannot be read"),
        (_present("rules: []\n"), "inline"),
        (_present("rules: [{stable_key: a, instruction: x}]\n"), "inline"),
        (_present("rules: [unclosed\n"), "errors"),
        (
            _present(
                "rules:\n  - stable_key: dup\n    instruction: One.\n  - stable_key: dup\n    instruction: Two.\n"
            ),
            "errors",
        ),
        (_present("- just\n- a list\n"), "errors"),
        (_present("version: 1"), "does not end with a newline"),
        # A repeated top-level key: the parser keeps the last, so a row added to the first would not take effect.
        (
            _present(
                "rules:\n  - stable_key: a\n    instruction: One.\nrules:\n  - stable_key: b\n    instruction: Two.\n"
            ),
            "by hand",
        ),
    ],
)
def test_a_base_that_cannot_be_patched_provably_is_refused_not_guessed(base: TrustedBaseDocument, why: str) -> None:
    with pytest.raises(RuleAdoptionError, match=why):
        propose_rule_adoption(_suggestion(), base)


def test_an_already_adopted_or_dismissed_or_oversized_suggestion_is_refused() -> None:
    base = _present(EXISTING)
    proposal = propose_rule_adoption(_suggestion(), base)
    landed = replace_content(base, proposal.proposed_content)
    with pytest.raises(RuleAdoptionError, match="already in the trusted base"):
        propose_rule_adoption(_suggestion(), landed)
    with pytest.raises(RuleAdoptionError, match="open suggestion"):
        propose_rule_adoption(_suggestion(state="dismissed"), base)
    with pytest.raises(RuleAdoptionError, match="by hand"):
        propose_rule_adoption(_suggestion(candidate_statement="x" * 4001), base)


@pytest.mark.parametrize(
    "statement",
    [
        "Quote \" and ' and \\ marks",
        "Colon: and # hash and - dash\nsecond line",
        "- stable_key: injected\n  instruction: pwn",
        "unicode " + chr(0x2028) + " separators and " + chr(0xE9),
        "{braces} [brackets] &anchor *alias !tag |pipe >fold",
    ],
)
def test_rule_text_cannot_break_out_of_its_row(tmp_path: Path, statement: str) -> None:
    base = _present(EXISTING)
    proposal = propose_rule_adoption(_suggestion(candidate_statement=statement), base)
    governance = _governance(replace_content(base, _apply(tmp_path, EXISTING, proposal.patch)))
    assert _keys(governance).count(proposal.stable_key) == 1 and len(governance.rules) == 3
    assert (
        next(row for row in governance.rules if row.stable_key == proposal.stable_key).instruction == statement.strip()
    )


def test_suggestions_from_repeated_feedback_are_adoptable_end_to_end() -> None:
    def note(annotation_id: str, review_id: str) -> Annotation:
        return Annotation(
            id=annotation_id,
            review_id=review_id,
            revision_id=f"rev-{review_id}",
            anchor=AnnotationAnchor(path="src/api/users.py", side="new", start_line=1, end_line=1),
            body=STATEMENT,
            source="human",
            created_by_actor="human",
            created_at="2026-10-01T10:00:00+00:00",
        )

    (suggestion,) = repeated_human_feedback_suggestions(
        (note("a1", "review-1"), note("a2", "review-2")), repository_scope="r"
    )
    proposal = propose_rule_adoption(suggestion, ABSENT)
    assert proposal.instruction == STATEMENT and proposal.paths == ("src/api/users.py",)
    assert proposal.supporting_feedback_refs == ("annotation:a1", "annotation:a2")
    # Proposing twice is the same patch: nothing was recorded by the first.
    assert propose_rule_adoption(suggestion, ABSENT) == proposal


KEEP = "rules:\n  - stable_key: a\n    instruction: |+\n      keep\n\n\nversion: 1\n"


def test_a_rule_row_never_changes_a_value_that_depends_on_blank_lines(tmp_path: Path) -> None:
    """``|+`` keeps its trailing blank lines: inserting before them would change rule ``a`` to ``keep\\n``."""

    base = _present(KEEP)
    proposal = propose_rule_adoption(_suggestion(), base)
    landed = _apply(tmp_path, KEEP, proposal.patch)
    assert yaml.safe_load(landed)["rules"][0] == {"stable_key": "a", "instruction": "keep\n\n\n"}
    assert [row["stable_key"] for row in yaml.safe_load(landed)["rules"]] == ["a", proposal.stable_key]
    # The patch is a pure insertion: dropping its added lines gives the base back, byte for byte.
    added = [
        line[1:]
        for line in proposal.patch.splitlines(keepends=True)
        if line.startswith("+") and not line.startswith("+++")
    ]
    assert landed.replace("".join(added), "", 1) == KEEP


def test_a_pure_insertion_that_cannot_keep_the_values_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both insertion points are checked against the parsed document, exactly; none passing means no proposal."""

    from lemoncrow.pro.capabilities.review import rule_suggestions as module

    original = module._insertions
    monkeypatch.setattr(
        module,
        "_insertions",
        lambda content, key, instruction, paths: original(content, key, instruction, paths)[:1],
    )
    with pytest.raises(RuleAdoptionError, match="by hand"):
        propose_rule_adoption(_suggestion(), _present(KEEP))
