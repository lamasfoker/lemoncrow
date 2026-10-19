from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lemoncrow.core.capabilities.session_comment_capture import capture_session_comment
from lemoncrow.core.foundation.paths import session_dir
from lemoncrow.pro.capabilities.review.session_models import ReviewBinding, ReviewSession
from lemoncrow.pro.capabilities.review.store import ReviewStore, new_session_id


def _events(root: Path, sid: str, host: str = "codex"):
    return json.loads((session_dir(root, host, sid) / "run.json").read_text())["events"]


def test_codex_stop_captures_commentary_and_final_but_excludes_reasoning_and_duplicate_stop(tmp_path: Path):
    transcript = tmp_path / "rollout.jsonl"
    rows = [
        {"type": "message", "role": "developer", "content": "You are operating as lemoncrow:review"},
        {"type": "message", "role": "assistant", "phase": "analysis", "content": [{"type": "text", "text": "private"}]},
        {
            "type": "message",
            "role": "assistant",
            "phase": "commentary",
            "content": [{"type": "text", "text": "Checking"}],
        },
        {
            "type": "message",
            "id": "reply",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": "The timeout case is missing."}],
        },
    ]
    transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    payload = {"session_id": "sid", "transcript_path": str(transcript), "cwd": str(tmp_path)}
    assert capture_session_comment(tmp_path / "store", payload, host="codex") == 2
    assert capture_session_comment(tmp_path / "store", payload, host="codex") == 0
    assert _events(tmp_path / "store", "sid")[0]["payload"]["message"] == "Checking"
    event = _events(tmp_path / "store", "sid")[1]
    assert event["payload"]["agent_role"] == "review"
    assert event["payload"]["message"] == "The timeout case is missing."
    assert len(_events(tmp_path / "store", "sid")) == 2


def test_partial_record_role_switch_and_child_identity(tmp_path: Path):
    transcript = tmp_path / "child.jsonl"
    first = {"type": "assistant", "message": {"id": "p1", "content": [{"type": "text", "text": "Plan one."}]}}
    transcript.write_text(json.dumps(first))
    payload = {
        "session_id": "parent",
        "agent_id": "child",
        "agent_role": "plan",
        "agent_transcript_path": str(transcript),
        "cwd": str(tmp_path),
    }
    root = tmp_path / "store"
    assert capture_session_comment(root, payload, host="claude") == 0
    with transcript.open("a") as stream:
        stream.write("\n")
    assert capture_session_comment(root, payload, host="claude") == 1
    second = {
        "type": "assistant",
        "message": {
            "id": "p2",
            "content": [{"type": "thinking", "thinking": "private"}, {"type": "text", "text": "Implemented."}],
        },
    }
    with transcript.open("a") as stream:
        stream.write(json.dumps(second) + "\n")
    payload["agent_role"] = "code"
    assert capture_session_comment(root, payload, host="claude") == 1
    events = _events(root, "child", "claude")
    assert [e["payload"]["agent_role"] for e in events] == ["plan", "code"]
    assert all(e["payload"]["parent_session_id"] == "parent" for e in events)
    assert not (root / "lemoncrow_reviews.db").exists()


def test_server_owned_review_binding_is_captured_from_workspace_marker(tmp_path: Path):
    root = tmp_path / "store"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=workspace, check=True, capture_output=True)

    stale_partition = root / "server" / "data" / "reviews" / "repo-stale"
    stale_store = ReviewStore(stale_partition)
    stale_session = stale_store.create_session(
        ReviewSession(
            id=new_session_id(),
            subject_type="local_change",
            repo_root="local:repo-stale",
            range_mode="working_tree",
        )
    )
    stale_store.bind_subject(
        ReviewBinding(
            id="",
            repository_identity="local:repo-stale",
            subject_kind="branch",
            subject_key="refs/heads/main",
            review_id=stale_session.id,
            worktree_key="stale-worktree",
        )
    )
    stale_marker = stale_partition / ".workspace-roots" / stale_session.id
    stale_marker.parent.mkdir(parents=True, exist_ok=True)
    stale_marker.write_text(str(workspace.resolve()) + "\n", encoding="utf-8")
    with stale_store._transaction() as conn:
        conn.execute(
            "UPDATE review_bindings SET created_at=?, updated_at=? WHERE review_id=?",
            ("2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00", stale_session.id),
        )

    partition = root / "server" / "data" / "reviews" / "repo-demo"
    store = ReviewStore(partition)
    session = store.create_session(
        ReviewSession(
            id=new_session_id(),
            subject_type="local_change",
            repo_root="local:repo-demo",
            range_mode="working_tree",
        )
    )
    store.bind_subject(
        ReviewBinding(
            id="",
            repository_identity="local:repo-demo",
            subject_kind="branch",
            subject_key="refs/heads/main",
            review_id=session.id,
            worktree_key="server-worktree",
        )
    )
    marker = partition / ".workspace-roots" / session.id
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(workspace.resolve()) + "\n", encoding="utf-8")

    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "id": "server-bound",
                    "content": [{"type": "output_text", "text": "Implemented the server-owned Review change."}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert (
        capture_session_comment(
            root,
            {"session_id": "codex-server", "transcript_path": str(transcript), "cwd": str(workspace)},
            host="codex",
        )
        == 1
    )
    context = _events(root, "codex-server")[0]["payload"]["review_context"]
    assert context["review_id"] == session.id
    assert context["repository_identity"] == "local:repo-demo"
    assert context["subject_kind"] == "branch"
    assert context["subject_key"] == "refs/heads/main"
    assert context["worktree_key"] == "server-worktree"


def test_codex_session_metadata_and_top_level_channel(tmp_path: Path):
    transcript = tmp_path / "rollout.jsonl"
    rows = [
        {"type": "session_meta", "payload": {"base_instructions": {"text": "You are operating as `lemoncrow:code`"}}},
        {"type": "turn_context", "payload": {"model": "gpt-fixture"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "id": "done",
                "content": [{"type": "output_text", "text": "Completed."}],
            },
        },
        {"type": "message", "role": "assistant", "channel": "analysis", "content": "private reasoning"},
        {
            "type": "response_item",
            "phase": "analysis",
            "payload": {"type": "message", "role": "assistant", "content": "private root phase"},
        },
        {"type": "reasoning", "role": "assistant", "content": "private reasoning record"},
    ]
    transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    root = tmp_path / "store"
    capture_session_comment(root, {"session_id": "sid", "transcript_path": str(transcript)}, host="codex")
    event = _events(root, "sid")[0]["payload"]
    assert (event["agent_role"], event["model"], event["message"]) == ("code", "gpt-fixture", "Completed.")
    assert len(_events(root, "sid")) == 1


@pytest.mark.parametrize("child", [False, True])
def test_claude_registered_lifecycle_entrypoints_capture_without_mcp(tmp_path: Path, child: bool):
    root = tmp_path / "store"
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {"id": "visible", "content": [{"type": "text", "text": "Hook-completed explanation"}]},
            }
        )
        + "\n"
    )
    payload = {
        "hook_event_name": "SubagentStop" if child else "Stop",
        "session_id": "parent",
        "transcript_path": str(transcript),
        "cwd": str(tmp_path),
        "agent_type": "lemoncrow:code",
    }
    if child:
        payload.update(agent_id="child", agent_transcript_path=str(transcript))
    script = "session_telemetry.py" if child else "stop.py"
    env = dict(os.environ, LEMONCROW_ROOT=str(root), LEMONCROW_AGENT="claude")
    subprocess.run(
        [sys.executable, "integrations/claude/plugin/hooks/" + script],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    events = _events(root, "child" if child else "parent", "claude")
    comments = [event["payload"] for event in events if event.get("payload", {}).get("completed")]
    assert len(comments) == 1 and comments[0]["agent_role"] == "code"


def test_codex_native_plan_mode_returns_to_previous_role(tmp_path: Path):
    transcript = tmp_path / "rollout.jsonl"
    root = tmp_path / "store"
    payload = {"session_id": "sid", "transcript_path": str(transcript)}
    rows = [
        {"type": "message", "role": "developer", "content": "You are operating as lemoncrow:code"},
        {"type": "turn_context", "payload": {"collaboration_mode": {"mode": "plan"}}},
        {"type": "message", "role": "assistant", "id": "plan", "content": "Plan complete"},
    ]
    transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    capture_session_comment(root, payload, host="codex")
    with transcript.open("a") as stream:
        for row in [
            {"type": "turn_context", "payload": {"collaboration_mode": {"mode": "default"}}},
            {"type": "message", "role": "assistant", "id": "code", "content": "Code complete"},
        ]:
            stream.write(json.dumps(row) + "\n")
    capture_session_comment(root, payload, host="codex")
    assert [event["payload"]["agent_role"] for event in _events(root, "sid")] == ["plan", "code"]


def test_incremental_capture_reads_only_appended_records_and_duplicate_stop_does_not_write(tmp_path: Path, monkeypatch):
    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(
        json.dumps({"type": "message", "role": "assistant", "id": "first", "content": "First reply " * 5000}) + "\n"
    )
    root = tmp_path / "store"
    payload = {"session_id": "sid", "transcript_path": str(transcript)}
    capture_session_comment(root, payload, host="codex")
    prior_offset = transcript.stat().st_size
    ledger = session_dir(root, "codex", "sid") / "run.json"
    before = ledger.stat().st_mtime_ns
    capture_session_comment(root, payload, host="codex")
    assert ledger.stat().st_mtime_ns == before
    with transcript.open("a") as stream:
        stream.write(json.dumps({"type": "message", "role": "assistant", "id": "next", "content": "Next reply"}) + "\n")
    original = Path.open

    class CheckedStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def readline(self):
            assert self.stream.tell() >= prior_offset, "old transcript records were scanned again"
            return self.stream.readline()

    def checked(path, *args, **kwargs):
        stream = original(path, *args, **kwargs)
        return CheckedStream(stream) if path == transcript and args == ("rb",) else stream

    monkeypatch.setattr(Path, "open", checked)
    assert capture_session_comment(root, payload, host="codex") == 1
    assert _events(root, "sid")[-1]["payload"]["message"] == "Next reply"


def test_all_visible_claude_messages_keep_role_at_emission_across_skill_switch(tmp_path: Path):
    transcript = tmp_path / "claude.jsonl"
    rows = [
        {
            "type": "assistant",
            "message": {
                "id": "before",
                "content": [
                    {"type": "text", "text": "Initial explanation"},
                    {"type": "tool_use", "name": "Skill", "input": {"skill": "lemoncrow:security-audit"}},
                    {"type": "thinking", "thinking": "private"},
                ],
            },
        },
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "raw tool output"}]}},
        {"type": "assistant", "message": {"id": "during", "content": [{"type": "text", "text": "Audit observation"}]}},
        {"type": "user", "message": {"content": "Continue"}},
        {"type": "assistant", "message": {"id": "after", "content": [{"type": "text", "text": "Final explanation"}]}},
    ]
    transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    root = tmp_path / "store"
    payload = {"session_id": "sid", "transcript_path": str(transcript), "agent_type": "UX Specialist"}
    assert capture_session_comment(root, payload, host="claude") == 3
    messages = [event["payload"] for event in _events(root, "sid", "claude")]
    assert [message["message"] for message in messages] == [
        "Initial explanation",
        "Audit observation",
        "Final explanation",
    ]
    assert [message["agent_role"] for message in messages] == ["UX Specialist", "security-audit", "security-audit"]
    assert capture_session_comment(root, payload, host="claude") == 0


@pytest.mark.parametrize("split_stop", [False, True])
def test_codex_mirrors_dedupe_without_dropping_distinct_repeated_messages(tmp_path: Path, split_stop: bool):
    transcript = tmp_path / "rollout.jsonl"
    response = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "id": "p1",
            "phase": "commentary",
            "content": [{"type": "output_text", "text": "Checking"}],
        },
    }
    mirror = {"type": "event_msg", "payload": {"type": "agent_message", "message": "Checking", "phase": "commentary"}}
    rows = [response] if split_stop else [response, mirror]
    transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    root = tmp_path / "store"
    payload = {"session_id": "sid", "transcript_path": str(transcript), "agent_type": "performance-auditor"}
    assert capture_session_comment(root, payload, host="codex") == 1
    with transcript.open("a") as stream:
        if split_stop:
            stream.write(json.dumps(mirror) + "\n")
        stream.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "id": "p2",
                        "content": [{"type": "output_text", "text": "Checking"}],
                    },
                }
            )
            + "\n"
        )
    assert capture_session_comment(root, payload, host="codex") == 1
    messages = [event["payload"] for event in _events(root, "sid")]
    assert [message["source_id"] for message in messages] == ["p1", "p2"]
    transcript.unlink()
    transcript.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                response,
                mirror,
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "id": "p2",
                        "content": [{"type": "output_text", "text": "Checking"}],
                    },
                },
            ]
        )
        + "\n"
    )
    assert capture_session_comment(root, payload, host="codex") == 0


def test_terminal_only_checkpoint_upgrades_and_recovers_skipped_messages(tmp_path: Path):
    transcript = tmp_path / "rollout.jsonl"
    rows = [
        {"type": "message", "role": "assistant", "id": "progress", "phase": "commentary", "content": "Progress"},
        {"type": "message", "role": "assistant", "id": "final", "content": "Final"},
    ]
    transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    root = tmp_path / "store"
    payload = {"session_id": "sid", "transcript_path": str(transcript)}
    assert capture_session_comment(root, payload, host="codex") == 2
    ledger = session_dir(root, "codex", "sid") / "run.json"
    data = json.loads(ledger.read_text())
    data["events"] = data["events"][1:]
    data["review_comment_capture"].pop("version")
    ledger.write_text(json.dumps(data))
    assert capture_session_comment(root, payload, host="codex") == 1
    assert {event["payload"]["message"] for event in _events(root, "sid")} == {"Progress", "Final"}
    assert capture_session_comment(root, payload, host="codex") == 0


def test_upgrade_recognizes_old_codex_idless_final_mirror(tmp_path: Path):
    import hashlib

    transcript = tmp_path / "rollout.jsonl"
    response = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "id": "reply",
            "content": [{"type": "output_text", "text": "Final"}],
        },
    }
    mirror = {
        "type": "event_msg",
        "timestamp": "2026-09-30T10:00:00Z",
        "payload": {"type": "agent_message", "message": "Final"},
    }
    rows = [
        {"type": "message", "role": "assistant", "id": "progress", "phase": "commentary", "content": "Progress"},
        response,
        mirror,
    ]
    transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    root = tmp_path / "store"
    payload = {"session_id": "sid", "transcript_path": str(transcript)}
    capture_session_comment(root, payload, host="codex")
    ledger = session_dir(root, "codex", "sid") / "run.json"
    data = json.loads(ledger.read_text())
    final = data["events"][-1]
    final["payload"]["event"] = "CompletedTurn"
    old_id = hashlib.sha256(b"Final").hexdigest()
    final["payload"]["source_id"] = old_id
    final["payload"]["visible_turn_id"] = hashlib.sha256(f"codex\0sid\0{old_id}\0Final".encode()).hexdigest()
    data["events"] = [final]
    data["review_comment_capture"].pop("version")
    ledger.write_text(json.dumps(data))
    assert capture_session_comment(root, payload, host="codex") == 1
    assert [event["payload"]["message"] for event in _events(root, "sid")].count("Final") == 1


def test_explicit_location_parser_keeps_references_and_rejects_external_urls():
    from lemoncrow.core.capabilities.session_comment_capture import finding_location, message_locations

    body = "Inspect `src/app.py:12-14` and [helper](/repo/helper.ts#L3-L5). Repeated `src/app.py:12-14`."
    assert [finding_location(item) for item in message_locations(body)] == [
        ("src/app.py", 12, 14),
        ("/repo/helper.ts", 3, 5),
    ]
    assert all(item["body"] == body for item in message_locations(body))
    assert not message_locations("See https://example.invalid/src/app.py:12 or https://src/app.py#L2")
    assert not message_locations("The code is around line 12.")


def test_transcript_rotation_replays_without_duplicate(tmp_path: Path):
    transcript = tmp_path / "rollout.jsonl"
    row = {"type": "event_msg", "payload": {"type": "agent_message", "id": "r1", "message": "Completed."}}
    transcript.write_text(json.dumps(row) + "\n")
    payload = {"session_id": "sid", "transcript_path": str(transcript), "cwd": str(tmp_path)}
    root = tmp_path / "store"
    assert capture_session_comment(root, payload, host="codex") == 1
    transcript.unlink()
    transcript.write_text(json.dumps(row) + "\n")
    assert capture_session_comment(root, payload, host="codex") == 0
    assert len(_events(root, "sid")) == 1
