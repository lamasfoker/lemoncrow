"""Spec §4.4 Routine eligibility: parser proofs, never a description."""

from __future__ import annotations

from typing import Any

from lemoncrow.pro.capabilities.review.routine import prove_file, routine_language, routine_proofs
from lemoncrow.pro.capabilities.review.targets import ReviewTarget, TargetSpan


def _target(target_id: str, path: str, *spans: tuple[str, int, int], kind: str = "hunk") -> ReviewTarget:
    return ReviewTarget(
        target_id=target_id,
        unit_key=f"{kind}:{path}:{target_id}",
        kind=kind,  # type: ignore[arg-type]
        path=path,
        label=target_id,
        symbol="",
        start_line=min(span[1] for span in spans) if spans else 0,
        end_line=max(span[2] for span in spans) if spans else 0,
        hunk_ordinals=(0,),
        spans=tuple(TargetSpan(side, start, end, 0) for side, start, end in spans),  # type: ignore[arg-type]
        state="unreviewed",
        changed_since_mark=False,
        reviewed_revision_id="",
        attention_rank=0,
        attention_level="normal",
        reasons=(),
        additions=1,
        deletions=0,
        fingerprint_method="exact",
    )


def _run(path: str, old: str | None, new: str | None, targets: list[ReviewTarget], **row: Any) -> dict[str, Any]:
    packet = {"files": [{"path": path, "old_path": None, "status": "modified", "category": "production", **row}]}
    texts = {"old": old, "new": new}
    proofs = routine_proofs(targets, packet, lambda _path, side: texts[side])
    return {target_id: proof.rule for target_id, proof in proofs.items()}


GO_OLD = "package p\n\nfunc f() {\n\tx := 1\n}\n"


def test_python_changes_that_are_not_routine() -> None:
    for old, new, spans in [
        ("def f():\n    return 1\n", "def f():\n\n    return 1\n", [("new", 2, 2)]),
        ('def f():\n    """Old."""\n', 'def f():\n    """New."""\n', [("old", 2, 2), ("new", 2, 2)]),
        ("x = f()\n", "x = f()  # type: ignore\n", [("old", 1, 1), ("new", 1, 1)]),
        ("x = f()\n", "x = f()  # noqa: E501\n", [("old", 1, 1), ("new", 1, 1)]),
        ("x = 1\n", "#!/usr/bin/env python\nx = 1\n", [("new", 1, 1)]),
    ]:
        assert _run("m.py", old, new, [_target("t", "m.py", *spans)]) == {}


def test_doc_and_directive_comments_never_qualify() -> None:
    for path, old, new, spans in [
        ("lib.rs", "fn f() {}\n", "/// Does f.\nfn f() {}\n", [("new", 1, 1)]),
        ("lib.rs", "fn f() {}\n", "//! Crate docs.\nfn f() {}\n", [("new", 1, 1)]),
        ("A.java", "class A {}\n", "/** Deprecated. */\nclass A {}\n", [("new", 1, 1)]),
        ("p.go", "package p\n", "//go:build linux\n\npackage p\n", [("new", 1, 2)]),
        ("a.js", "const a = 1;\n", "// eslint-disable-next-line\nconst a = 1;\n", [("new", 1, 1)]),
        ("a.ts", "const a: number = f();\n", "// @ts-expect-error\nconst a: number = f();\n", [("new", 1, 1)]),
        ("b.rs", "fn f() {}\n", "// SAFETY: the pointer is valid\nfn f() {}\n", [("new", 1, 1)]),
    ]:
        assert _run(path, old, new, [_target("t", path, *spans)]) == {}


def test_only_the_comment_target_of_a_mixed_file_is_routine() -> None:
    old = "package p\n\nfunc f() int {\n\treturn 1\n}\n\nfunc g() int {\n\treturn 2\n}\n"
    new = "package p\n\nfunc f() int {\n\treturn 10\n}\n\nfunc g() int {\n\t// two\n\treturn 2\n}\n"
    targets = [_target("t-f", "p.go", ("old", 4, 4), ("new", 4, 4)), _target("t-g", "p.go", ("new", 8, 8))]
    assert _run("p.go", old, new, targets) == {"t-g": "comment_only"}


def test_files_that_are_never_routine() -> None:
    for row in [
        {"status": "renamed", "old_path": "q.go"},
        {"status": "added"},
        {"status": "deleted"},
        {"category": "generated"},
        {"category": "vendor"},
        {"category": "config"},
        {"is_binary": True},
    ]:
        new = "package p\n\nfunc f() {\n\t// explain x\n\tx := 1\n}\n"
        assert _run("p.go", GO_OLD, new, [_target("t", "p.go", ("new", 4, 4))], **row) == {}


def test_prove_file_names_the_rule_and_language() -> None:
    new = "package p\n\nfunc f() {\n\t// explain x\n\tx := 1\n}\n"
    (proof,) = prove_file("p.go", GO_OLD, new, [_target("t", "p.go", ("new", 4, 4))]).values()
    assert (proof.path, proof.rule, proof.language) == ("p.go", "comment_only", "go")
    assert "Comment-only" in proof.detail


# --------------------------------------------------------------------------- #
# Phase 3a final review: position-aware proofs (C1, C2, I1-I4)
# --------------------------------------------------------------------------- #


def test_routine_language_contract() -> None:
    assert {path: routine_language(path) for path in ("a/b.go", "a/b.tsx", "a/b.py", "a/b.rb", "a/b.yaml")} == {
        "a/b.go": "go",
        "a/b.tsx": "tsx",
        "a/b.py": "python",
        "a/b.rb": "",
        "a/b.yaml": "",
    }


def test_routine_positive_contract() -> None:
    cases = [
        ("p.go", "package p\n\nfunc f() {\n  x := 1\n}\n", GO_OLD, [("old", 4, 4), ("new", 4, 4)], "formatting_only"),
        ("a.ts", "const a = {x:1}\n", "const a = { x: 1 }\n", [("old", 1, 1), ("new", 1, 1)], "formatting_only"),
        ("p.go", GO_OLD, "package p\n\nfunc f() {\n\t// explain x\n\tx := 1\n}\n", [("new", 4, 4)], "comment_only"),
        ("m.py", "def f():\n    return 1\n", "def f():\n    # why\n    return 1\n", [("new", 2, 2)], "comment_only"),
        ("m.py", "x = f()\n", "x = f()  # cached upstream\n", [("old", 1, 1), ("new", 1, 1)], "comment_only"),
    ]
    for path, old, new, spans, rule in cases:
        assert _run(path, old, new, [_target("t", path, *spans)]) == {"t": rule}, path
    assert (
        _run("a.ts", "const a = {x:1}\n", "const a = { x: 1 };\n", [_target("t", "a.ts", ("old", 1, 1), ("new", 1, 1))])
        == {}
    )


def test_ineligible_file_contract() -> None:
    old = "def up():\n    pass\n"
    new = "def up():\n    # note\n    pass\n"
    cases = [
        ("app/migrations/0002_add.py", old, new, [("new", 2, 2)]),
        ("a.rb", "x = 1\n", "# c\nx = 1\n", [("new", 1, 1)]),
        ("m.py", None, "# c\n", [("new", 1, 1)]),
        ("p.go", "package p\n\nfunc f( {\n", "// c\npackage p\n\nfunc f( {\n", [("new", 1, 1)]),
        (
            "p.go",
            'package p\n\nimport "C"\n\nfunc f() {}\n',
            'package p\n\nimport "C"\n\n// f does nothing\nfunc f() {}\n',
            [("new", 5, 5)],
        ),
    ]
    for path, before, after, spans in cases:
        assert _run(path, before, after, [_target("t", path, *spans)]) == {}, path
    big = "x = 1\n" * 200_000
    assert _run("m.py", big, "# c\n" + big, [_target("t", "m.py", ("new", 1, 1))]) == {}


def test_semantic_reordering_and_directive_changes_are_never_routine() -> None:
    cases = [
        ("a.js", "function f(){ return x }\n", "function f(){ return\nx }\n", [("old", 1, 1), ("new", 1, 2)]),
        ("m.py", "init()\nrun()\n", "run()\ninit()  # after run\n", [("old", 1, 1), ("new", 2, 2)]),
        ("m.js", "setup();\nmain();\n", "main();\nsetup(); // after main\n", [("old", 1, 1), ("new", 2, 2)]),
        ("a.js", "const x = typeof/* c */y;\n", "const x = typeofy;\n", [("old", 1, 1), ("new", 1, 1)]),
        ("a.ts", "function f() { return/* c */x; }\n", "function f() { returnx; }\n", [("old", 1, 1), ("new", 1, 1)]),
        (
            "A.java",
            "class A { int f() { return/* c */x; } }\n",
            "class A { int f() { returnx; } }\n",
            [("old", 1, 1), ("new", 1, 1)],
        ),
        ("m.py", "x = 1\n", "x = 1  # note\revil()\n", [("old", 1, 1), ("new", 1, 1)]),
        ("a.js", "let x = 1;\n", "let x = 1; // note\u2028evil();\n", [("old", 1, 1), ("new", 1, 1)]),
        ("A.java", "class A {}\n", "class A {} // note \\u000a static { evil(); }\n", [("old", 1, 1), ("new", 1, 1)]),
    ]
    for path, old, new, spans in cases:
        assert _run(path, old, new, [_target("t", path, *spans)]) == {}, path

    steps = "".join(f"    step{i}()\n" for i in range(10))
    old = "def f():\n    charge_card()\n" + steps + "    # done\n"
    new = "def f():\n    # start\n" + steps + "    charge_card()\n"
    target = _target("sym", "m.py", ("old", 2, 2), ("old", 13, 13), ("new", 2, 2), ("new", 13, 13), kind="symbol")
    assert _run("m.py", old, new, [target]) == {}

    go_old = 'package p\n\nimport _ "embed"\n\n//go:embed secret.txt\nvar A string\n\nvar B string\n'
    go_new = 'package p\n\nimport _ "embed"\n\nvar A string\n\n//go:embed secret.txt\nvar B string\n'
    assert _run("p.go", go_old, go_new, [_target("t", "p.go", ("old", 5, 8), ("new", 5, 8))]) == {}
    ts_old = "// @ts-expect-error\nconst a: number = f();\nconst b: number = g();\n"
    ts_new = "const a: number = f();\n// @ts-expect-error\nconst b: number = g();\n"
    assert _run("a.ts", ts_old, ts_new, [_target("t", "a.ts", ("old", 1, 3), ("new", 1, 3))]) == {}

    plain_old = 'package p\n\nimport _ "embed"\n\n//go:embed a.txt\nvar A string\nvar B string\n'
    plain_new = 'package p\n\nimport _ "embed"\n\n// assets\nvar A string\n//go:embed a.txt\nvar B string\n'
    assert _run("p.go", plain_old, plain_new, [_target("t", "p.go", ("old", 5, 7), ("new", 5, 8))]) == {}

    cgo_old = 'package p\n\nimport (\n\t// #include <stdio.h>\n\t"C"\n)\n\nfunc f() {}\n'
    cgo_new = cgo_old.replace("stdio", "stdlib")
    assert _run("p.go", cgo_old, cgo_new, [_target("t", "p.go", ("old", 4, 4), ("new", 4, 4))]) == {}
    tabbed = 'package p\n\n// #include <stdio.h>\nimport\t"C"\n'
    assert (
        _run("p.go", tabbed, tabbed.replace("stdio", "stdlib"), [_target("t", "p.go", ("old", 3, 3), ("new", 3, 3))])
        == {}
    )

    lossy = 'class A { String s = "caf�"; }\n'
    assert _run("A.java", lossy, lossy, [_target("t", "A.java", ("old", 1, 1), ("new", 1, 1))]) == {}
