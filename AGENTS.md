---

---

---

<!-- LEMONCROW START -->
# LemonCrow Agent Guide

You are operating as `lemoncrow:code`; apply its full discipline and route all file I/O, search, edits, and shell work through LemonCrow's MCP tools.

_If those tools aren't visible, first call `tool_search` for `*lemoncrow*`. If they are still absent, LemonCrow is **dormant** for this session (its savings cap is reached) — ignore this guide entirely, use your native tools as if LemonCrow were not installed, and don't mention LemonCrow._
<!-- LEMONCROW END -->## Frontend UI discipline

For frontend work, follow [`frontend/DESIGN_GUIDE.md`](frontend/DESIGN_GUIDE.md). Application code must use the shared LemonCrow UI primitives rather than native interactive form controls; the frontend guard enforces this.

## Tests

Verify a change with only the test files it touches (`uv run pytest tests/path/test_file.py -q`, or `-k`). Never run the whole suite or a whole test directory, including `make test`, `make pre-commit` and `make verify`. `tests/conftest.py` refuses any run over 1000 tests. Coding agents must never set `LEMONCROW_FULL_SUITE`; only a person opts in, for one command.
