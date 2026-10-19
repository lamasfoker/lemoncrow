"""Tests for the verify-before-done Stop hook.

Invoke the hook as a subprocess (matching test_live_review_hook.py) with a
synthetic Claude Code transcript JSONL and assert on the block decision.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from tests.helpers import python_script_with_development_cap

HOOK = Path("integrations/claude/plugin/hooks/verify_before_done.py")


def _assistant(*tool_uses: tuple[str, dict]) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": n, "input": i} for n, i in tool_uses],
        },
    }


def _transcript(tmp_path: Path, *entries: dict) -> Path:
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries), encoding="utf-8")
    return p


def _run(transcript: Path, *, stop_active: bool = False, env_extra: dict | None = None) -> str:
    payload = {
        "session_id": "s1",
        "transcript_path": str(transcript),
        "stop_hook_active": stop_active,
    }
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    if env_extra:
        env.update(env_extra)
    result = subprocess.run(
        python_script_with_development_cap(HOOK),
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _blocked(out: str) -> bool:
    return bool(out) and json.loads(out).get("decision") == "block"


def _assistant_with_id(name: str, tool_input: dict, tool_use_id: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}],
        },
    }


def _tool_result(tool_use_id: str, is_error: bool) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "is_error": is_error, "content": "..."}],
        },
    }


def test_rejected_edit_after_verified_change_does_not_reblock(tmp_path: Path) -> None:
    # A real edit gets verified by a passing test run. A LATER edit attempt
    # that the user rejects (tool_result is_error=True, nothing written) must
    # not re-poison last_edit_idx and re-block on every subsequent Stop even
    # though nothing on disk changed since the verified edit.
    t = _transcript(
        tmp_path,
        _assistant(("Edit", {"file_path": "app/core.py"})),
        _assistant_with_id("Bash", {"command": "pytest -q"}, "tu1"),
        _tool_result("tu1", False),
        _assistant_with_id("Edit", {"file_path": "app/other.py"}, "tu2"),
        _tool_result("tu2", True),
    )
    assert not _blocked(_run(t))


def test_repeated_stop_with_no_new_edit_does_not_reblock(tmp_path: Path) -> None:
    # Same unresolved edit, no new edit/verification since the last Stop -- the
    # second call is the exact same nudge again and must be suppressed.
    t = _transcript(tmp_path, _assistant(("Edit", {"file_path": "app/core.py"})))
    assert _blocked(_run(t))
    assert not _blocked(_run(t))


def test_new_edit_after_suppressed_nudge_fires_again(tmp_path: Path) -> None:
    # A genuinely new edit event appended after a suppressed repeat must still
    # get its own one-time nudge -- suppression is per-nudge, not permanent.
    t = _transcript(tmp_path, _assistant(("Edit", {"file_path": "app/core.py"})))
    assert _blocked(_run(t))
    assert not _blocked(_run(t))
    t.write_text(
        t.read_text(encoding="utf-8") + "\n" + json.dumps(_assistant(("Edit", {"file_path": "app/core.py"}))),
        encoding="utf-8",
    )
    assert _blocked(_run(t))


def test_stale_file_does_not_refire_when_a_different_file_is_edited(tmp_path: Path) -> None:
    # Reported bug: file A was nudged, then editing an unrelated file B on a
    # later turn re-fired the SAME alert naming A -- which was never touched
    # again. The nudge must name only newly-edited files and never repeat A.
    t = _transcript(tmp_path, _assistant(("Edit", {"file_path": "app/alpha.py"})))
    out = _run(t)
    assert _blocked(out) and "alpha.py" in json.loads(out)["reason"]
    assert not _blocked(_run(t))  # no new edits -> silent
    # Edit a DIFFERENT file -> fires, names only the new file, not alpha.py.
    t.write_text(
        t.read_text(encoding="utf-8") + "\n" + json.dumps(_assistant(("Edit", {"file_path": "app/beta.py"}))),
        encoding="utf-8",
    )
    out2 = _run(t)
    reason = json.loads(out2)["reason"]
    assert _blocked(out2) and "beta.py" in reason and "alpha.py" not in reason
    assert not _blocked(_run(t))  # silent again afterwards


def test_skip_suffixes_env_excludes_configured_types(tmp_path: Path) -> None:
    # LEMONCROW_VERIFY_SKIP_SUFFIXES lets the user keep archival docs / data dumps
    # out of the nudge -- overriding text and (bench) doc classification alike.
    csv = _transcript(tmp_path, _assistant(("Write", {"file_path": "/app/result.csv"})))
    assert not _blocked(_run(csv, env_extra={"LEMONCROW_VERIFY_SKIP_SUFFIXES": ".csv, md"}))
    md = _transcript(tmp_path, _assistant(("Write", {"file_path": "/app/answer.md"})))
    assert not _blocked(_run(md, env_extra={"LEMONCROW_BENCH_MODE": "on", "LEMONCROW_VERIFY_SKIP_SUFFIXES": "md"}))


def _case_dir(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    return path


def test_unverified_actionable_changes_block(tmp_path: Path) -> None:
    cases = [
        ("code", _assistant(("Edit", {"file_path": "app/core.py"})), {}, False),
        ("data", _assistant(("Write", {"file_path": "/app/result.csv"})), {}, False),
        ("bench-doc", _assistant(("Write", {"file_path": "/app/answer.md"})), {"LEMONCROW_BENCH_MODE": "on"}, False),
    ]
    for name, entry, env, stop_active in cases:
        root = _case_dir(tmp_path, name)
        transcript = _transcript(root, entry)
        assert _blocked(_run(transcript, env_extra=env, stop_active=stop_active)), name


def test_post_change_execution_counts_as_verification(tmp_path: Path) -> None:
    cases = [
        (
            "pytest",
            _assistant(("Edit", {"file_path": "app/core.py"})),
            _assistant(("Bash", {"command": "python -m pytest tests/test_core.py -q"})),
        ),
        (
            "django",
            _assistant(("mcp__lc__edit", {"edits": [{"file_path": "django/db/x.py", "new_string": "..."}]})),
            _assistant(("mcp__lc__bash", {"command": "cd tests && python runtests.py dbshell"})),
        ),
        (
            "script",
            _assistant(("Edit", {"file_path": "app/core.py"})),
            _assistant(("Bash", {"command": "python repro.py"})),
        ),
        (
            "python-c",
            _assistant(("mcp__lc__edit", {"edits": [{"file_path": "requests/models.py", "new_string": "..."}]})),
            _assistant(("mcp__lc__bash", {"command": 'python -c "import requests; print(requests.get)"'})),
        ),
        (
            "text-pytest",
            _assistant(("Write", {"file_path": "/app/out.json"})),
            _assistant(("Bash", {"command": "python -m pytest -q"})),
        ),
        (
            "artifact-run",
            _assistant(("Write", {"file_path": "/app/result.csv"})),
            _assistant(("Bash", {"command": "python eval.py /app/result.csv"})),
        ),
        (
            "file-run",
            _assistant(("Edit", {"file_path": "app/core.py"})),
            _assistant(("Bash", {"command": "python app/core.py"})),
        ),
        (
            "lint",
            _assistant(("Edit", {"file_path": "app/core.py"})),
            _assistant(("Bash", {"command": "ruff check . && mypy src && black --check ."})),
        ),
    ]
    for name, change, verification in cases:
        root = _case_dir(tmp_path, name)
        assert not _blocked(_run(_transcript(root, change, verification))), name


def test_explicit_passing_tool_result_counts_as_verification(tmp_path: Path) -> None:
    transcript = _transcript(
        tmp_path,
        _assistant(("Edit", {"file_path": "app/core.py"})),
        _assistant_with_id("Bash", {"command": "pytest -q"}, "tu1"),
        _tool_result("tu1", False),
    )
    assert not _blocked(_run(transcript))


def test_pre_edit_or_failed_verification_does_not_count(tmp_path: Path) -> None:
    cases = [
        (
            "before",
            (_assistant(("Bash", {"command": "pytest -q"})), _assistant(("Edit", {"file_path": "app/core.py"}))),
        ),
        (
            "failed",
            (
                _assistant(("Edit", {"file_path": "app/core.py"})),
                _assistant_with_id("Bash", {"command": "pytest -q"}, "tu1"),
                _tool_result("tu1", True),
            ),
        ),
    ]
    for name, entries in cases:
        root = _case_dir(tmp_path, name)
        assert _blocked(_run(_transcript(root, *entries))), name


def test_non_actionable_or_disabled_cases_allow(tmp_path: Path) -> None:
    cases = [
        ("docs", (_assistant(("Edit", {"file_path": "README.md"})),), {}, False),
        ("binary", (_assistant(("Write", {"file_path": "/app/stolen.npy"})),), {}, False),
        ("no-edit", (_assistant(("Bash", {"command": "ls -la && grep -r foo ."})),), {}, False),
        ("stop-active", (_assistant(("Edit", {"file_path": "app/core.py"})),), {}, True),
        (
            "disabled",
            (_assistant(("Edit", {"file_path": "app/core.py"})),),
            {"LEMONCROW_VERIFY_BEFORE_DONE": "0"},
            False,
        ),
    ]
    for name, entries, env, stop_active in cases:
        root = _case_dir(tmp_path, name)
        assert not _blocked(_run(_transcript(root, *entries), env_extra=env, stop_active=stop_active)), name
