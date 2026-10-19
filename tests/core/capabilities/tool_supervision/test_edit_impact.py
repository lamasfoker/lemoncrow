"""Unit tests for post-edit contract-literal discovery (edit_impact).

The feature surfaces the *other* files that still reference a quoted contract
literal (config key, wire field, kwarg name) an edit removed, so a rename or
deletion is finished at every parallel consumer -- not just the file handed to
the agent. These consumers have no call-graph edge to the edited site, so
symbol-level callers/callees never find them.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from lemoncrow.pro.capabilities.tool_supervision import edit_impact
from lemoncrow.pro.capabilities.tool_supervision.edit_impact import (
    LITERAL_SCAN_TRUNCATED,
    LITERAL_SCAN_UNPARSED,
    SIGNATURE_SCAN_TRUNCATED,
    SYMBOL_SCAN_TRUNCATED,
    _combine_matches,
    _is_structural_occurrence,
    _scan_literals,
    contract_literal_impact,
    literal_replacements,
    removed_literals,
)


def _astgrep_available() -> bool:
    try:
        from lemoncrow_client.kit.astgrep import AstGrepToolUnavailable

        from lemoncrow.infra.code_intel.astgrep import astgrep_adapter

        try:
            astgrep_adapter(Path(".")).search(pattern='"x"', language="python", limit=1)
        except AstGrepToolUnavailable:
            return False
        return True
    except Exception:
        return True


_requires_astgrep = pytest.mark.skipif(not _astgrep_available(), reason="ast-grep binary unavailable")
_APOSTROPHE_BLOB = '"""Session store."""\n\n\ndef state_path(root, session_id):\n    """Resolve the daemon\'s state file.\n\n    Restarting the daemon\'s worker pool drops in-flight work.\n    """\n    # The supervisor\'s queue is drained before the root is rebound.\n    return root / "sessions" / session_id / "state.json"\n\n\ndef load(conn, session_id):\n    return conn.execute(\n        "SELECT payload FROM traces WHERE json_extract(payload, \'$.session_id\') = ?",\n        (session_id,),\n    ).fetchone()\n\n\nROUTES = {"legacy_key": handle_legacy, "kept_key": handle_kept}\n'


def test_edit_impact_literal_scan_contract() -> None:
    """case: test_apostrophe_in_docstring_does_not_desynchronise_the_scan"""
    'One genuinely removed literal -> exactly one finding, and it is the real one.\n\n    On the regex this replaced, this same edit produced exactly one finding too --\n    but it was ``"\': handle_legacy, \'"``, a slice of code between two literals that\n    exists in no file, while the real removal of ``"legacy_key"`` went unreported.\n    Both halves matter: an invented finding is worse than a missed one, and the\n    parser must not buy its precision by dropping the true positive.\n    '
    new = _APOSTROPHE_BLOB.replace(
        '{"legacy_key": handle_legacy, "kept_key": handle_kept}', '{"kept_key": handle_kept}'
    )
    edits = [{"path": "src/store.py", "old_string": _APOSTROPHE_BLOB, "new_string": new}]
    assert literal_replacements(edits) == {"legacy_key": None}
    "case: test_no_finding_is_a_fragment_of_the_file_it_came_from"
    scan = _scan_literals(_APOSTROPHE_BLOB, "src/store.py")
    assert not scan.reason
    assert "legacy_key" in scan.literals
    assert "sessions" in scan.literals
    for literal in scan.literals:
        assert f'"{literal}"' in _APOSTROPHE_BLOB or f"'{literal}'" in _APOSTROPHE_BLOB, literal
    "case: test_quoted_text_in_a_comment_is_not_a_literal"
    blob = "# the 'passwd' key was the old name\nKEY = 'real_key'\n"
    assert _scan_literals(blob, "src/a.py").literals == frozenset({"real_key"})
    "case: test_docstring_prose_is_not_a_contract_literal"
    blob = 'def f():\n    """Return the session id."""\n    return KEYS["session_id"]\n'
    assert _scan_literals(blob, "src/a.py").literals == frozenset({"session_id"})
    "case: test_a_blob_that_is_only_a_literal_is_the_literal_under_edit"
    assert literal_replacements([{"file_path": "src/a.py", "old_string": '"passwd"', "new_string": '"password"'}]) == {
        "passwd": "password"
    }
    "case: test_rich_edit_range_and_symbol_suffixes_still_resolve_the_language"
    edits = [{"path": "src/a.ts:L1-L2", "old_string": "const k = 'legacy_key';\n", "new_string": "const k = 'kept';\n"}]
    assert literal_replacements(edits) == {"legacy_key": "kept"}


def test_edit_impact_degraded_parse_contract() -> None:
    """case: test_unparsable_python_reports_nothing_and_says_so"""
    scan = _scan_literals("KEYS = {'a': 'legacy_key',\n", "src/a.py")
    assert scan.literals == frozenset()
    assert scan.reason == LITERAL_SCAN_UNPARSED
    "case: test_unparsable_side_is_skipped_rather_than_reported_as_removal"
    edits = [{"path": "src/a.py", "old_string": "KEYS = {'legacy_key': 1}\n", "new_string": "KEYS = {\n"}]
    assert literal_replacements(edits) == {}
    "case: test_contract_literal_impact_names_the_degradation"
    edits = [{"path": "src/a.py", "old_string": "KEYS = {'legacy_key': 1}\n", "new_string": "KEYS = {\n"}]
    impact = contract_literal_impact(edits, engine=None, repo_root=Path("."), touched_paths=["src/a.py"])
    assert impact is not None
    assert impact["sites"] == []
    assert impact["degraded"] == [LITERAL_SCAN_UNPARSED]
    "case: test_non_python_blob_is_read_with_its_own_grammar"
    blob = "// the 'legacy_key' name is gone\nconst k = 'real_key';\nconst t = `route ${id} end`;\n"
    assert _scan_literals(blob, "src/a.ts").literals == frozenset({"real_key"})
    "case: test_unknown_language_reports_no_literal_and_a_reason"
    scan = _scan_literals('key = "legacy_key"\n', "vendor/uv.lock")
    assert scan.literals == frozenset()
    assert scan.reason == LITERAL_SCAN_UNPARSED


def test_edit_impact_rename_contract() -> None:
    """case: test_line_aligned_swap_is_detected_as_rename"""
    edits = [{"old_string": "params['passwd'] = value", "new_string": "params['password'] = value"}]
    assert literal_replacements(edits) == {"passwd": "password"}
    "case: test_removed_without_clear_replacement_maps_to_none"
    edits = [{"old_string": "a = 'passwd'\nb = 1", "new_string": "b = 1\nc = 2"}]
    repl = literal_replacements(edits)
    assert repl.get("passwd") is None
    assert removed_literals(edits) == ["passwd"]
    "case: test_additive_edit_removes_nothing"
    edits = [{"old_string": "x = 1", "new_string": "x = 1\ny = 'new_key'"}]
    assert literal_replacements(edits) == {}
    assert removed_literals(edits) == []
    "case: test_noisy_and_short_literals_are_ignored"
    edits = [{"old_string": "a='1'; b='true'; c='q'", "new_string": "a='2'; b='false'; c='z'"}]
    assert literal_replacements(edits) == {}
    "case: test_pure_move_within_edit_is_not_a_removal"
    edits = [{"old_string": "f('database', x)", "new_string": "g(x, 'database')"}]
    assert "database" not in literal_replacements(edits)
    "case: test_non_string_descriptors_are_skipped"
    edits = [{"kind": "symbol", "name": "foo", "new_body": "def foo(): return 'passwd'"}]
    assert literal_replacements(edits) == {}


def test_edit_impact_structural_contract() -> None:
    """case: test_structural_occurrence_true_for_code_keys"""
    for line in ["settings['passwd'] = env", "value = cfg.get('passwd')", "{'passwd': 1}", "'passwd': value,"]:
        assert _is_structural_occurrence(line, "passwd") is True
    "case: test_combine_keeps_astgrep_code_and_adds_only_noncode_text"
    astgrep = [("db/client.py", 10, "settings['passwd']")]
    text = [
        ("db/client.py", 99, "# duplicate code-file hit from text -- drop"),
        ("conf/settings.ini", 4, "passwd = '...'"),
    ]
    combined = _combine_matches(astgrep, text)
    paths = {p for p, _, _ in combined}
    assert paths == {"db/client.py", "conf/settings.ini"}
    assert ("db/client.py", 10, "settings['passwd']") in combined
    assert ("db/client.py", 99, "# duplicate code-file hit from text -- drop") not in combined
    "case: test_combine_uses_pure_text_when_astgrep_unavailable"
    text = [("db/client.py", 10, "settings['passwd']")]
    assert _combine_matches(None, text) == text


def test_edit_impact_decorator_contract() -> None:
    """case: test_decorator_removal_surfaces_cache_method_usages"""
    from lemoncrow.pro.capabilities.tool_supervision.edit_impact import decorator_contract_impact

    engine = _FakeEngine(
        {"get_resolver.cache_clear": [_FakeMatch("django/urls/base.py", 95, "    get_resolver.cache_clear()")]}
    )
    edits = [
        {
            "old_string": "@functools.lru_cache(maxsize=None)\ndef get_resolver(urlconf=None):\n    return x\n",
            "new_string": "def get_resolver(urlconf=None):\n    return _get_cached_resolver(urlconf)\n",
        }
    ]
    sites = decorator_contract_impact(edits, engine=engine, touched_paths=["django/urls/resolvers.py"])
    assert sites, "removed @lru_cache with a .cache_clear caller elsewhere must surface a site"
    paths = {s["path"] for s in sites}
    assert any(p.startswith("django/urls/base.py") for p in paths)
    assert any("cache_clear" in s["new"] for s in sites)
    "case: test_decorator_kept_on_helper_does_not_flag_helper"
    from lemoncrow.pro.capabilities.tool_supervision.edit_impact import decorator_contract_impact

    engine = _FakeEngine({})
    edits = [
        {
            "old_string": "@functools.lru_cache(maxsize=None)\ndef get_resolver(urlconf=None):\n    return x\n",
            "new_string": "def get_resolver(urlconf=None):\n    return _cached(urlconf)\n\n\n@functools.lru_cache(maxsize=None)\ndef _cached(urlconf=None):\n    return x\n",
        }
    ]
    sites = decorator_contract_impact(edits, engine=engine, touched_paths=["django/urls/resolvers.py"])
    assert sites == []


@pytest.mark.parametrize("line", ["the 'passwd' field is legacy and unused", "note that 'passwd' was renamed"])
def test_structural_occurrence_false_for_prose(line: str) -> None:
    assert _is_structural_occurrence(line, "passwd") is False


def test_astgrep_detection_batches_rules_and_normalizes_scan_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.infra.code_intel import astgrep

    consumer = tmp_path / "db" / "client.py"
    _write(tmp_path, "db/client.py", "value = cfg['passwd']\n")

    class _FakeAdapter:
        scan_calls = 0

        def __init__(self, _repo_root: Path) -> None:
            pass

        def scan(self, *, rules: list[dict], no_ignore: bool, limit: int) -> SimpleNamespace:
            type(self).scan_calls += 1
            assert no_ignore is False
            assert len(rules) == 2
            assert limit == edit_impact._ASTGREP_SCAN_MATCH_CEILING
            return SimpleNamespace(
                matches=[
                    SimpleNamespace(
                        rule_id="lc-impact-0", file_path=str(consumer), line=0, snippet="value = cfg['passwd']"
                    )
                ],
                truncated=False,
            )

        def search(self, **_kwargs: object) -> None:
            raise AssertionError("batched rule-mode should avoid per-pattern ast-grep processes")

    monkeypatch.setattr(astgrep, "astgrep_adapter", _FakeAdapter)
    detection = edit_impact._astgrep_detect(["passwd"], tmp_path, {"db/base.py"}, languages=["python"], limit=30)
    assert _FakeAdapter.scan_calls == 1
    assert detection is not None
    assert detection.by_literal == {"passwd": [("db/client.py", 1, "value = cfg['passwd']")]}
    assert detection.truncated is False
    assert edit_impact._ASTGREP_SCAN_MATCH_CEILING == 20000


def test_common_candidate_cannot_starve_a_rare_one_out_of_the_batched_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One global slice over file-ordered output is not a per-candidate budget.

    ``scan`` truncates with a single ``raw_matches[:limit]``, so an aggregate
    allowance handed to a batch of rules is spent by whichever candidate matches
    first in file order. A candidate whose only consumer sorts late then comes
    back with zero rows and ``_combine_matches`` -- which treats a non-``None``
    ast-grep result as authoritative for code files -- discards its text hits
    too, so the reviewer is told about no sites at all.
    """
    from lemoncrow.infra.code_intel import astgrep

    for index in range(150):
        _write(tmp_path, f"app/common_{index:03d}.py", "flag = 'legacy_mode'\n")
    _write(tmp_path, "zz/rare.py", "name = 'zz_rare_contract'\n")

    class _FakeAdapter:

        def __init__(self, _repo_root: Path) -> None:
            pass

        def scan(self, *, rules: list[dict], no_ignore: bool, limit: int) -> SimpleNamespace:
            raw = [
                SimpleNamespace(
                    rule_id="lc-impact-0",
                    file_path=str(tmp_path / f"app/common_{index:03d}.py"),
                    line=0,
                    snippet="flag = 'legacy_mode'",
                )
                for index in range(150)
            ]
            raw.append(
                SimpleNamespace(
                    rule_id="lc-impact-2",
                    file_path=str(tmp_path / "zz/rare.py"),
                    line=0,
                    snippet="name = 'zz_rare_contract'",
                )
            )
            return SimpleNamespace(matches=raw[:limit], truncated=len(raw) > limit)

        def search(self, **_kwargs: object) -> None:
            raise AssertionError("batched rule-mode should avoid per-pattern ast-grep processes")

    monkeypatch.setattr(astgrep, "astgrep_adapter", _FakeAdapter)
    detection = edit_impact._astgrep_detect(
        ["legacy_mode", "zz_rare_contract"], tmp_path, {"db/base.py"}, languages=["python"], limit=30
    )
    assert detection is not None
    assert detection.by_literal["zz_rare_contract"] == [("zz/rare.py", 1, "name = 'zz_rare_contract'")]
    assert len(detection.by_literal["legacy_mode"]) == 30


def test_capped_astgrep_scan_is_reported_as_degraded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A scan that hit the materialisation ceiling is partial, not complete."""
    monkeypatch.setattr(
        edit_impact,
        "_astgrep_detect",
        lambda *a, **k: edit_impact._AstGrepDetection(by_literal={"passwd": []}, truncated=True),
    )
    edits = [{"old_string": "d['passwd']", "new_string": "d['password']"}]
    impact = contract_literal_impact(edits, engine=None, repo_root=tmp_path, touched_paths=["db/base.py"])
    assert impact is not None
    assert impact["sites"] == []
    assert impact["degraded"] == [LITERAL_SCAN_TRUNCATED]


def _capped(truncated: bool, key: str) -> object:
    detection = edit_impact._AstGrepDetection(by_literal={key: []}, truncated=truncated)
    return lambda *_args, **_kwargs: detection


def test_capped_symbol_scan_is_disclosed_as_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A removed symbol whose scan was capped is unproven, not clean.

    ``symbol_contract_impact`` returns a bare site list, so above the ceiling an
    empty return says "nothing still references this" with exactly the confidence
    of an exhaustive pass -- the silent-partial-result defect the literal pass was
    already fixed for.
    """
    edits = [{"path": "pkg/shared.py", "old_string": "def load_config(path):\n    return path\n", "new_string": ""}]
    monkeypatch.setattr(edit_impact, "_astgrep_detect", _capped(True, "load_config"))
    capped = edit_impact.symbol_contract_impact(edits, engine=None, repo_root=tmp_path, touched_paths=["pkg/shared.py"])
    assert list(capped) == []
    assert capped.degraded == (SYMBOL_SCAN_TRUNCATED,)
    assert isinstance(capped, list)
    monkeypatch.setattr(edit_impact, "_astgrep_detect", _capped(False, "load_config"))
    complete = edit_impact.symbol_contract_impact(
        edits, engine=None, repo_root=tmp_path, touched_paths=["pkg/shared.py"]
    )
    assert complete.degraded == (), "a pass that looked everywhere must not claim to be partial"


def test_capped_signature_scan_is_disclosed_as_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same claim for the call-site pass: a capped scan is not 'no caller breaks'."""
    edits = [
        {
            "path": "pkg/session.py",
            "old_string": "def refresh_session(token):\n    return token\n",
            "new_string": "def refresh_session(token, scope):\n    return token\n",
        }
    ]
    monkeypatch.setattr(edit_impact, "_astgrep_detect", _capped(True, "refresh_session"))
    capped = edit_impact.signature_change_impact(
        edits, engine=None, repo_root=tmp_path, touched_paths=["pkg/session.py"]
    )
    assert list(capped) == []
    assert capped.degraded == (SIGNATURE_SCAN_TRUNCATED,)
    monkeypatch.setattr(edit_impact, "_astgrep_detect", _capped(False, "refresh_session"))
    complete = edit_impact.signature_change_impact(
        edits, engine=None, repo_root=tmp_path, touched_paths=["pkg/session.py"]
    )
    assert complete.degraded == (), "a pass that looked everywhere must not claim to be partial"


def _write(root: Path, rel: str, body: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


@_requires_astgrep
def test_surfaces_parallel_consumer_and_excludes_touched_and_prose(tmp_path: Path) -> None:
    _write(tmp_path, "db/base.py", "def get_connection_params(d):\n    return {'passwd': d['passwd']}\n")
    _write(tmp_path, "db/client.py", "def settings_to_args(d):\n    return ['--password', d['passwd']]\n")
    _write(tmp_path, "db/legacy.py", "# the 'passwd' key was the old name; do not flag this comment\nX = 1\n")
    edits = [{"old_string": "{'passwd': d['passwd']}", "new_string": "{'password': d['password']}"}]
    impact = contract_literal_impact(edits, engine=None, repo_root=tmp_path, touched_paths=["db/base.py"])
    assert impact is not None
    assert impact["reason"]
    sites = impact["sites"]
    passwd_sites = [s for s in sites if s["old"] == "passwd"]
    assert passwd_sites
    assert passwd_sites[0]["new"] == "password"
    all_paths = {s["path"] for s in sites}
    assert any(p.startswith("db/client.py") for p in all_paths)
    assert not any(p.startswith("db/base.py") for p in all_paths)
    assert not any(p.startswith("db/legacy.py") for p in all_paths)


@_requires_astgrep
def test_none_when_literal_occurs_nowhere_else(tmp_path: Path) -> None:
    _write(tmp_path, "only.py", "X = {'solo_key': 1}\n")
    edits = [{"old_string": "{'solo_key': 1}", "new_string": "{'renamed_key': 1}"}]
    impact = contract_literal_impact(edits, engine=None, repo_root=tmp_path, touched_paths=["only.py"])
    assert impact is None


class _FakeMatch:

    def __init__(self, file_path: str, line: int, text: str) -> None:
        self.file_path = file_path
        self.line = line
        self.text = text


class _FakeEngine:
    """Minimal _TextSearcher: returns canned hits keyed by the quoted query."""

    def __init__(self, by_query: dict[str, list[_FakeMatch]]) -> None:
        self._by_query = by_query

    def search_text(self, query: str, *, path: str = ".", limit: int = 50, ignore_case: bool = False) -> list:
        return self._by_query.get(query, [])


def test_removed_private_symbol_does_not_match_unrelated_module_namesake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for PR #25's first ATTENTION finding.

    ``edit_impact.py`` removed its private ``_QUOTED_LITERAL_RE`` while
    ``bash_exec.py`` happened to define a different private symbol with the same
    spelling. A bare repository search merged the two and told the reviewer the
    removal had surviving references. Python private names may be shared inside
    one package, but an unrelated package is not evidence for this definition.
    """
    monkeypatch.setattr(edit_impact, "_astgrep_detect", lambda *a, **k: None)
    engine = _FakeEngine(
        {
            "_QUOTED_LITERAL_RE": [
                _FakeMatch(
                    "src/lemoncrow/pro/capabilities/tool_supervision/bash_exec.py",
                    66,
                    "_QUOTED_LITERAL_RE = re.compile('other')",
                )
            ]
        }
    )
    edits = [
        {
            "path": "src/lemoncrow/pro/capabilities/review/edit_impact.py",
            "old_string": "_QUOTED_LITERAL_RE = re.compile('old')\n",
            "new_string": "",
        }
    ]
    assert (
        edit_impact.symbol_contract_impact(
            edits,
            engine=engine,
            repo_root=tmp_path,
            touched_paths=["src/lemoncrow/pro/capabilities/review/edit_impact.py"],
        )
        == []
    )


def test_removed_private_symbol_still_finds_sibling_package_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(edit_impact, "_astgrep_detect", lambda *a, **k: None)
    engine = _FakeEngine({"_emit": [_FakeMatch("pkg/consumer.py", 4, "return _emit(value)")]})
    edits = [{"path": "pkg/shared.py", "old_string": "def _emit(value):\n    return value\n", "new_string": ""}]
    sites = edit_impact.symbol_contract_impact(
        edits, engine=engine, repo_root=tmp_path, touched_paths=["pkg/shared.py"]
    )
    assert sites and sites[0]["path"] == "pkg/consumer.py:L4"


def test_text_fallback_recall_when_astgrep_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(edit_impact, "_astgrep_detect", lambda *a, **k: None)
    engine = _FakeEngine(
        {
            "'passwd'": [
                _FakeMatch("conf/db.cfg", 3, "value = config['passwd']"),
                _FakeMatch("docs/notes.md", 7, "the 'passwd' option is legacy"),
            ]
        }
    )
    edits = [{"old_string": "d['passwd']", "new_string": "d['password']"}]
    impact = contract_literal_impact(edits, engine=engine, repo_root=tmp_path, touched_paths=["db/base.py"])
    assert impact is not None
    paths = {s["path"] for s in impact["sites"]}
    assert any(p.startswith("conf/db.cfg") for p in paths)
    assert not any(p.startswith("docs/notes.md") for p in paths)
