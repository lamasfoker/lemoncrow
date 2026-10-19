from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from lemoncrow.infra.runtime.run_ledger import RunLedger
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.journal import collect_review_journal
from lemoncrow.pro.capabilities.review.models import ProvenanceRecord, ReviewJournalEntry, review_packet_from_dict
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs


def _ledger(root: Path, repo: Path) -> RunLedger:
    ledger = RunLedger(
        session_id="session-review-journal",
        agent="claude",
        root=root,
        task="Refactor checkout without changing user-visible behavior",
        workspace_path=repo,
    )
    ledger.set_plan(["Trace checkout callers", "Change the shared validator", "Run checkout tests"])
    ledger.add_verified_fact("The API and web checkout share validate_cart().")
    ledger.add_hypothesis("The bug is in currency normalization.", rejected=True)
    ledger.add_open_question("Does mobile send legacy coupon fields?")
    ledger.set_blocker("Waiting on the contract fixture")
    ledger.set_next_validation("Run the checkout API integration suite")
    ledger.record_file_event("src/checkout.py", "edit", "@@ -1 +1 @@\n-old\n+new")
    ledger.record_file_event("docs/unrelated.md", "edit", "@@ -1 +1 @@\n-a\n+b")
    ledger.record_command("pytest tests/checkout", True, stdout="18 passed")
    ledger.record_test("checkout-contract", True, "schema remained compatible")
    ledger.record_tool_call(
        "code_search",
        {"query": "validate_cart", "paths": ["src/checkout.py"]},
        "src/checkout.py: validate_cart called by checkout",
    )
    ledger.record(
        "agent_message",
        "assistant: checkout validation is now shared",
        {
            "role": "assistant",
            "message": "I updated checkout validation and the focused contract test passes.",
            "event": "VisibleTurn",
        },
    )
    ledger.record(
        "agent_message",
        "assistant: credential-bearing visible text",
        {
            "role": "assistant",
            "message": "token=supersecret-journal-value",
            "event": "VisibleTurn",
        },
    )
    ledger.record(
        "reasoning",
        "PRIVATE_REASONING_SUMMARY_MUST_NOT_LEAK",
        {"text": "PRIVATE_REASONING_DETAIL_MUST_NOT_LEAK"},
    )
    # RunLedger may retain raw model-call fields for runtime accounting. Review
    # journal must never expose them as a substitute for explicit rationale.
    ledger.record_call(
        operation="private-reasoning-probe",
        model="gpt-test",
        input_tokens=10,
        output_tokens=5,
        prompt="SECRET_RAW_MODEL_PROMPT_MUST_NOT_LEAK",
        response="SECRET_RAW_MODEL_RESPONSE_MUST_NOT_LEAK",
    )
    ledger.persist()
    return ledger


def _provenance(
    *, host: str = "claude", session_id: str = "session-review-journal", certainty: str = "exact"
) -> ProvenanceRecord:
    return ProvenanceRecord(
        status="matched",
        host=host,
        session_id=session_id,
        certainty=certainty,  # type: ignore[arg-type]
        match_reason="exact authoring session",
    )


def test_review_journal_projects_observable_relevant_author_context(tmp_path: Path) -> None:
    root = tmp_path / "store"
    repo = tmp_path / "repo"
    repo.mkdir()
    _ledger(root, repo)

    journal = collect_review_journal(root, repo, _provenance(), ["src/checkout.py"])

    assert any(item.kind == "plan" and "Trace checkout callers" in item.detail for item in journal)
    assert any(item.kind == "fact" and "share validate_cart" in item.summary for item in journal)
    assert any(item.kind == "decision" and "currency normalization" in item.summary for item in journal)
    assert any(item.kind == "question" and "legacy coupon" in item.summary for item in journal)
    assert any(item.kind == "blocker" and "contract fixture" in item.summary for item in journal)
    assert any(item.kind == "validation" and "Next validation" in item.summary for item in journal)
    assert any(item.kind == "edit" and item.path == "src/checkout.py" for item in journal)
    assert not any(item.path == "docs/unrelated.md" for item in journal)
    assert any(item.kind == "command" and item.status == "pass" for item in journal)
    assert any(item.kind == "validation" and item.status == "pass" for item in journal)
    assert any(item.kind == "tool" and item.path == "src/checkout.py" for item in journal)
    assert any(item.kind == "message" and "focused contract test passes" in item.summary for item in journal)
    rendered = repr(journal)
    assert "PRIVATE_REASONING" not in rendered
    assert "supersecret-journal-value" not in rendered
    assert "<redacted-credential>" in rendered


def test_review_journal_never_projects_raw_llm_prompt_or_response(tmp_path: Path) -> None:
    root = tmp_path / "store"
    repo = tmp_path / "repo"
    repo.mkdir()
    _ledger(root, repo)

    journal = collect_review_journal(root, repo, _provenance(), ["src/checkout.py"])
    rendered = repr(journal)

    assert "SECRET_RAW_MODEL_PROMPT_MUST_NOT_LEAK" not in rendered
    assert "SECRET_RAW_MODEL_RESPONSE_MUST_NOT_LEAK" not in rendered
    assert "private-reasoning-probe" not in rendered


def test_review_journal_requires_exact_host_session_and_workspace(tmp_path: Path) -> None:
    root = tmp_path / "store"
    repo = tmp_path / "repo"
    repo.mkdir()
    _ledger(root, repo)

    assert collect_review_journal(root, repo, _provenance(host="codex"), ["src/checkout.py"]) == ()
    assert collect_review_journal(root, repo, _provenance(session_id="no-such-session"), ["src/checkout.py"]) == ()
    assert collect_review_journal(root, repo, _provenance(certainty="heuristic"), ["src/checkout.py"]) == ()

    other_repo = tmp_path / "other"
    other_repo.mkdir()
    assert collect_review_journal(root, other_repo, _provenance(), ["src/checkout.py"]) == ()


def test_review_packet_journal_round_trips_as_revision_data(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    import pygit2

    git = pygit2.init_repository(str(repo), initial_head="main")
    git.config["user.name"] = "Journal Test"
    git.config["user.email"] = "journal@example.test"
    (repo / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    git.index.add_all()
    git.index.write()
    tree = git.index.write_tree()
    sig = pygit2.Signature("Journal Test", "journal@example.test", 1700000000, 0)
    git.create_commit("HEAD", sig, sig, "base", tree, [])
    (repo / "app.py").write_text("def value():\n    return 2\n", encoding="utf-8")

    build = build_review_packet_with_blobs(
        repo,
        resolve_rev_range(repo, working_tree=True),
        store_root=tmp_path / "store",
        with_impact=False,
        with_provenance=False,
        with_patch_text=True,
    )
    entry = ReviewJournalEntry(
        id="jrnl-roundtrip",
        kind="decision",
        at="2026-09-23T18:00:00+00:00",
        summary="Keep the public contract unchanged",
        detail="The shared validator is the intended seam.",
        path="app.py",
        source="run_ledger",
        host="claude",
        session_id="session-roundtrip",
    )
    packet = replace(build.packet, journal=(entry,))

    restored = review_packet_from_dict(packet.to_dict())

    assert restored.journal == (entry,)
