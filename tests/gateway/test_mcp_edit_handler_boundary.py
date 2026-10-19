from __future__ import annotations

from lemoncrow.gateway.adapters import mcp_server


def test_edit_hooks_late_bind_worktree_and_caps(monkeypatch) -> None:
    marker = object()

    def fake_worktree(_root):
        return marker

    monkeypatch.setattr(mcp_server, "_session_worktree_root", fake_worktree)
    monkeypatch.setattr(mcp_server, "_EDIT_DIAG_CAP", 3)
    monkeypatch.setattr(mcp_server, "_EDIT_VCS_CAP", 4)
    hooks = mcp_server._edit_handler_hooks()
    assert hooks.session_worktree_root is fake_worktree
    assert hooks.session_worktree_root(None) is marker
    assert hooks.edit_diag_cap == 3
    assert hooks.edit_vcs_cap == 4
