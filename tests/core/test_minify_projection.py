"""Tests for the tree-sitter minified source projection (read view + mapped edits)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.source_projection import (
    MinifiedEditError,
    apply_minified_edit,
    build_minified_projection,
    language_for_minify,
    minified_line_gutter,
)
from lemoncrow.pro.capabilities.tool_supervision.rich_edit import apply_rich_edits

PY_SAMPLE = '"""Module docstring."""\nimport os\n\n\ndef add(a, b):\n    """Return the sum."""\n    # inline note\n    label = "keep   inner   spaces"\n    return a + b\n\n\nclass Worker:\n    """Worker doc."""\n\n    value = 1\n\n    def run(self):\n        return add(self.value, 2)\n'


def _reconstructs(result: Any) -> bool:
    rebuilt = "".join(result.content[s.projected_start : s.projected_end] for s in result.mapping.segments)
    return bool(rebuilt == result.content)


def test_projection_content_contract() -> None:
    """case: test_strips_comments_and_blanks_keeps_strings_and_docstrings"""
    r = build_minified_projection(PY_SAMPLE, "python", path="m.py", include_mapping=True)
    assert r.applied
    assert "Module docstring" in r.content
    assert "Return the sum" in r.content
    assert "Worker doc" in r.content
    assert "# inline note" not in r.content
    assert "keep   inner   spaces" in r.content
    assert "\n\n\n" not in r.content
    assert r.saved_tokens > 0
    "case: test_docstrings_preserved_when_minifying"
    src = 'def stub():\n    """Only docstring."""\n\n\ndef real():\n    # x\n    return 1\n'
    r = build_minified_projection(src, "python")
    assert r.applied
    assert "Only docstring" in r.content
    assert "# x" not in r.content
    "case: test_unicode_is_byte_safe"
    src = 'def f():\n    # café ☕\n    return "naïve"\n'
    r = build_minified_projection(src, "python", include_mapping=True)
    assert r.applied and _reconstructs(r)
    assert "café" not in r.content
    assert 'return "naïve"' in r.content
    "case: test_go_minifies_without_indentation"
    src = 'package   main\n\n// greet\nfunc   Greet()   {\n\tprintln(   "hi"   )\n}\n'
    r = build_minified_projection(src, "go", path="g.go", include_mapping=True)
    assert r.applied and _reconstructs(r)
    assert "// greet" not in r.content
    assert 'println( "hi" )' in r.content
    "case: test_excluded_language_skips"
    r = build_minified_projection("# Title\n\nsome   prose\n", "markdown")
    assert not r.applied
    "case: test_language_for_minify_resolution"
    assert language_for_minify("a.py") == "python"
    assert language_for_minify("a.go") == "go"
    assert language_for_minify("a.md") is None
    assert language_for_minify("a.unknownext") is None


def test_projection_mapping_contract() -> None:
    """case: test_mapping_reconstructs_and_is_idempotent"""
    r = build_minified_projection(PY_SAMPLE, "python", path="m.py", include_mapping=True)
    assert r.mapping is not None and r.mapping.projection_kind == "minified"
    assert _reconstructs(r)
    again = build_minified_projection(r.content, "python")
    assert not again.applied or again.content == r.content


def test_edit_round_trip_contract() -> None:
    """case: test_edit_round_trip_python_preserves_disk_comment_and_docstring"""
    updated, line_start, line_end = apply_minified_edit(PY_SAMPLE, "python", "return a + b", "return a + b + 1")
    assert "return a + b + 1" in updated
    assert "# inline note" in updated
    assert "Return the sum" in updated
    assert line_start == line_end
    "case: test_edit_round_trip_go"
    src = 'package main\n\nfunc Greet(name string) {\n\tprintln("hi", name)  // x\n}\n'
    updated, _, _ = apply_minified_edit(src, "go", 'println("hi", name)', 'println("hello", name)')
    assert 'println("hello", name)' in updated
    assert "// x" in updated


def test_edit_refusal_contract() -> None:
    """case: test_edit_ambiguous_raises"""
    src = "def f():\n    # note\n    x = 1\n    x = 1\n    return x\n"
    with pytest.raises(MinifiedEditError) as exc:
        apply_minified_edit(src, "python", "x = 1", "x = 2")
    assert exc.value.code == "ambiguous"
    "case: test_edit_no_match_raises"
    with pytest.raises(MinifiedEditError) as exc:
        apply_minified_edit(PY_SAMPLE, "python", "nonexistent_token_xyz", "z")
    assert exc.value.code == "no_match"
    "case: test_edit_dropped_interior_fails_closed"
    src = "def f():\n    a = 1\n    # critical\n    b = 2\n    return a + b\n"
    with pytest.raises(MinifiedEditError) as exc:
        apply_minified_edit(src, "python", "a = 1\n    b = 2", "a = 10\n    b = 20")
    assert exc.value.code == "comment_inside_span"


def test_rich_edit_minified_contract() -> None:
    """case: test_rich_edit_minified_fallback_applies"""
    disk = "def compute():\n    total     =     0\n    return total\n"
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "m.py"
        f.write_text(disk, encoding="utf-8")
        res = apply_rich_edits(
            [{"file_path": str(f), "old_string": "total = 0", "new_string": "total = 42"}], repo_root=d
        )
        assert not res["rolled_back"]
        assert res["applied"][0]["match_mode"] == "minified"
        after = f.read_text(encoding="utf-8")
        assert "total = 42" in after
        assert "total     =" not in after
    "case: test_rich_edit_minified_fails_closed_preserves_comment"
    disk = "def f():\n    x = 1\n    # critical\n    y = 2\n    return x + y\n"
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "n.py"
        f.write_text(disk, encoding="utf-8")
        res = apply_rich_edits(
            [{"file_path": str(f), "old_string": "x = 1\n    y = 2", "new_string": "x = 10\n    y = 20"}], repo_root=d
        )
        assert res["rolled_back"]
        assert "# critical" in f.read_text(encoding="utf-8")


def test_edit_fuzzy_fallback_recovers_near_miss_reconstruction() -> None:
    src = 'import subprocess\nfrom pathlib import Path\n\n\ndef make_repo(tmp_path: Path) -> Path:\n    root = tmp_path / "repo"\n    root.mkdir()\n    subprocess.run(["git", "init", "-q"], cwd=root, check=True)\n    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True)  \n    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)\n    return root\n'
    old_string = 'def make_repo(tmp_path: Path) -> Path:\n    root = tmp_path / "repo"\n    root.mkdir()\n    subprocess.run(["git", "init", "-q"], cwd=root, check=True)\n    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=root, check=True)\n    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)\n    return root'
    new_string = old_string.replace("return root", "return root.resolve()")
    updated, line_start, line_end = apply_minified_edit(src, "python", old_string, new_string)
    assert "return root.resolve()" in updated
    assert updated.count("subprocess.run") == 3
    assert line_start <= line_end


def test_edit_fuzzy_ambiguous_in_minified_space_falls_back_not_crashes() -> None:
    src = "import os\nimport sys\nimport re\nimport json\n\n\ndef helper():\n    x = 1\n    y = 2\n    z = 3\n    return x + y + z\n\n\n# a trailing module note kept only to give the minifier real savings\nVERSION = 1\n"
    old_string = (
        "import os\nimport sys\nimport json\n\n\ndef helper():\n    x = 1\n    y = 2\n    z = 3\n    return x + y + z"
    )
    with pytest.raises(MinifiedEditError) as exc:
        apply_minified_edit(src, "python", old_string, old_string + "  # x")
    assert exc.value.code == "ambiguous"


def test_minified_line_gutter_resolves_line_within_multiline_segment() -> None:
    src = "def f():\n    '''Line one.\n\n    Line three.\n    Line four.\n    '''\n    # strip me\n    a = 1\n"
    result = build_minified_projection(src, "python", include_mapping=True, path="ds.py")
    assert result.applied and result.mapping is not None
    gutter = minified_line_gutter(result.content, result.mapping)
    disk_lines = src.splitlines()
    for gline in gutter.splitlines():
        num_str, _, text = gline.partition("\t")
        disk_line = disk_lines[int(num_str) - 1]
        assert text or disk_line.strip() == ""
        assert disk_line.strip() == text.strip()
    numbers = [int(gline.partition("\t")[0]) for gline in gutter.splitlines()]
    assert numbers == sorted(set(numbers))
    assert numbers == [1, 2, 3, 4, 5, 6, 8]
