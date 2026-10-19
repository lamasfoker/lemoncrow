"""Forward-ported MINIFY hardening: comment-atomicity, per-language atomics, registry.

Covers the three hardening changes:

1. ``_COMMENT_TYPES`` nodes are captured atomically in ``_collect_tokens`` so
   structured doc comments (rust/scala) no longer leak inner markers into the
   inter-token gap as "non-whitespace bytes between tokens".
2. ``_LANG_EXTRA_ATOMIC`` makes html ``doctype`` and bash ``command`` nodes
   atomic per-language (so ``<!DOCTYPE html>`` unnamed content and bash
   backslash-newline continuations stay verbatim) without affecting other
   languages.
3. html/css/lua are registered in the language table.

The fail-closed-on-parse-error and revalidation guards, newline preservation,
and the no-ASI invariant are all asserted to survive.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.infra.code_intel.languages import ALL_LANGUAGES, LANGUAGES, language_by_name, language_for_path
from lemoncrow.pro.capabilities.source_projection import build_minified_projection
from lemoncrow.pro.capabilities.source_projection.minify import _parser_for

_CORPUS = Path(__file__).resolve().parent / "_corpus"


def _reparses_clean(content: str, lang: str) -> bool:
    parser = _parser_for(lang)
    assert parser is not None, f"no grammar for {lang}"
    root = parser.parse(content.encode("utf-8")).root_node
    return root is not None and (not root.has_error)


@pytest.mark.parametrize(("name", "extensions"), [("html", {".html", ".htm"}), ("css", {".css"}), ("lua", {".lua"})])
def test_new_language_records_registered(name: str, extensions: set[str]) -> None:
    record = language_by_name(name)
    assert record is not None
    assert record.name == name == record.parser_name
    assert record.parser_name is not None
    assert set(record.extensions) == extensions
    assert name in ALL_LANGUAGES
    for ext in extensions:
        resolved = language_for_path(f"file{ext}")
        assert resolved is not None and resolved.name == name


RUST_SAMPLE = '//! Crate-level doc comment.\n//! Spans two lines.\n\nuse std::fmt;\n\n/// A worker that does work.\n///\n/// # Examples\n/// ```\n/// let w = Worker::new(1);\n/// ```\npub struct Worker {\n    id: u32,\n}\n\nimpl Worker {\n    /* block comment\n       across lines */\n    pub fn new(id: u32) -> Self {\n        // line comment\n        let label = "keep   inner   spaces";\n        let _ = label;\n        Worker { id }\n    }\n}\n'
SCALA_SAMPLE = 'package demo\n\n/** A worker.\n *  @param id the worker id\n *  @return nothing useful\n */\nclass Worker(id: Int) {\n  // run it once\n  def run(): Int = {\n    val label = "keep   inner   spaces"\n    id + label.length\n  }\n}\n'


def test_minify_language_hardening_contract() -> None:
    """case: test_rust_with_doc_and_block_comments_minifies"""
    r = build_minified_projection(RUST_SAMPLE, "rust", path="w.rs")
    assert r.applied, r.reason
    assert r.projected_tokens < r.original_tokens
    assert _reparses_clean(r.content, "rust")
    assert "//! Crate-level" not in r.content
    assert "/// A worker" not in r.content
    assert "block comment" not in r.content
    assert "// line comment" not in r.content
    assert "keep   inner   spaces" in r.content
    "case: test_scala_with_doc_comment_minifies"
    r = build_minified_projection(SCALA_SAMPLE, "scala", path="w.scala")
    assert r.applied, r.reason
    assert r.projected_tokens < r.original_tokens
    assert _reparses_clean(r.content, "scala")
    assert "@param" not in r.content
    assert "// run it once" not in r.content
    assert "keep   inner   spaces" in r.content
    "case: test_html_doctype_and_text_minifies"
    r = build_minified_projection(HTML_SAMPLE, "html", path="page.html")
    assert r.applied, r.reason
    assert r.projected_tokens < r.original_tokens
    assert _reparses_clean(r.content, "html")
    assert "<!DOCTYPE html>" in r.content
    assert "<!-- a stripped comment -->" not in r.content
    assert "<script>" in r.content and "const x = 1;" in r.content
    assert "Pool   Overview" in r.content
    "case: test_bash_heredoc_and_line_continuation_minifies"
    r = build_minified_projection(BASH_SAMPLE, "bash", path="build.sh")
    assert r.applied, r.reason
    assert r.projected_tokens < r.original_tokens
    assert _reparses_clean(r.content, "bash")
    assert "# configure the build" not in r.content
    assert "\\\n" in r.content
    assert "this   heredoc   body" in r.content
    assert "is preserved verbatim" in r.content
    "case: test_extra_atomic_is_per_language_not_global"
    py = "# c\ndef command(x):\n    return  x +  1\n"
    r = build_minified_projection(py, "python", path="m.py")
    assert r.applied, r.reason
    assert _reparses_clean(r.content, "python")
    assert "# c" not in r.content
    assert "return x + 1" in r.content
    "case: test_newlines_preserved_no_asi"
    js = "// c\nconst a = 1\nconst b = 2\nconst c = a + b\n"
    r = build_minified_projection(js, "javascript", path="m.js")
    assert r.applied, r.reason
    assert _reparses_clean(r.content, "javascript")
    assert "const a = 1\nconst b = 2" in r.content
    assert ";" not in r.content


def test_minify_language_coverage_contract() -> None:
    """case: test_at_least_twenty_languages_minify_on_clean_snippets"""
    succeeded: list[str] = []
    for lang, src in CLEAN_SNIPPETS.items():
        r = build_minified_projection(src, lang)
        if r.applied and r.projected_tokens < r.original_tokens and _reparses_clean(r.content, lang):
            succeeded.append(lang)
    assert len(succeeded) >= 20, f"only {len(succeeded)} minified: {sorted(succeeded)}"
    assert {"html", "css", "lua"}.issubset(succeeded)
    "case: test_language_table_has_at_least_twenty_records"
    assert len(LANGUAGES) >= 20
    assert {"html", "css", "lua"}.issubset(ALL_LANGUAGES)
    "case: test_kotlin_corpus_fails_closed_gracefully"
    f = _CORPUS / "tmp.unmin.kt"
    if not f.exists():
        pytest.skip("kotlin corpus fixture unavailable")
    text = f.read_text()
    r = build_minified_projection(text, "kotlin", path=str(f))
    assert not r.applied
    assert r.content == text
    assert r.projected_tokens == r.original_tokens
    assert r.reason
    "case: test_scss_as_css_corpus_fails_closed_gracefully"
    f = _CORPUS / "tmp.unmin.css"
    if not f.exists():
        pytest.skip("css corpus fixture unavailable")
    text = f.read_text()
    r = build_minified_projection(text, "css", path=str(f))
    assert not r.applied
    assert r.content == text
    assert r.reason


HTML_SAMPLE = '<!DOCTYPE html>\n<html lang="en">\n  <head>\n    <title>Dashboard</title>\n  </head>\n  <body>\n    <!-- a stripped comment -->\n    <h1>Pool   Overview</h1>\n    <p>Some inline   text content here.</p>\n    <script>\n      const x = 1;\n      console.log(x);\n    </script>\n  </body>\n</html>\n'
BASH_SAMPLE = "#!/bin/bash\n# configure the build\nconfigure --prefix=/usr \\\n  --enable-foo \\\n  --enable-bar\n\ncat <<'EOF'\n  this   heredoc   body\n  is preserved verbatim\nEOF\n\necho done\n"
CLEAN_SNIPPETS: dict[str, str] = {
    "python": "# c\ndef f(a, b):\n    return a + b\n",
    "typescript": "// c\nfunction f(a: number) {\n  return a + 1;\n}\n",
    "javascript": "// c\nfunction f(a) {\n  return a + 1;\n}\n",
    "bash": "# c\necho hello\nls -la\n",
    "csharp": "// c\nclass A {\n  int X() { return 1; }\n}\n",
    "go": "// c\npackage main\nfunc f() int {\n\treturn 1\n}\n",
    "rust": "// c\nfn f() -> i32 {\n    1\n}\n",
    "java": "// c\nclass A {\n  int x() { return 1; }\n}\n",
    "scala": "// c\nobject A {\n  def f(): Int = 1\n}\n",
    "ruby": "# c\ndef f\n  1\nend\n",
    "cpp": "// c\nint f() {\n  return 1;\n}\n",
    "c": "int f(void) {\n  return 1;\n}\n",
    "swift": "// c\nfunc f() -> Int {\n  return 1\n}\n",
    "php": "<?php\n// c\nfunction f() {\n  return 1;\n}\n",
    "sql": "-- c\nSELECT a,\n       b\nFROM t;\n",
    "yaml": "# c\nkey: value\nlist:\n  - a\n  - b\n",
    "toml": '# c\nkey = "value"\n[section]\nx = 1\n',
    "json": '{\n  "a": 1,\n  "b": 2\n}\n',
    "html": "<!DOCTYPE html>\n<html>\n  <body>\n    <p>hi</p>\n  </body>\n</html>\n",
    "css": "/* c */\n.a {\n  color: red;\n}\n",
    "lua": "-- c\nlocal function f()\n  return 1\nend\n",
}


@pytest.mark.parametrize(("ext", "lang"), [("rs", "rust"), ("html", "html"), ("sh", "bash")])
def test_corpus_targets_apply_and_reparse(ext: str, lang: str) -> None:
    f = _CORPUS / f"tmp.unmin.{ext}"
    if not f.exists():
        pytest.skip(f"corpus fixture tmp.unmin.{ext} unavailable")
    text = f.read_text()
    r = build_minified_projection(text, lang, path=str(f))
    assert r.applied, r.reason
    assert r.projected_tokens < r.original_tokens
    assert _reparses_clean(r.content, lang)
    assert "\n" in r.content
