from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from lemoncrow_server_core.errors import ErrorCode, ServerError
from lemoncrow_server_core.index.contracts import ManifestEntry
from lemoncrow_server_core.review_derivation import derive_review_packet_from_views, review_range_from_wire

from lemoncrow.pro.capabilities.review.gitdiff import RevRange
from lemoncrow.pro.capabilities.review.models import ReviewPacket


class _Content:
    def __init__(self, values: dict[str, bytes]) -> None:
        self.values = values

    def get(self, org_id: str, digest: str) -> bytes | None:
        assert org_id == "org-a"
        return self.values.get(digest)


def _entry(path: str, payload: bytes, *, mode: int = 0o100644) -> ManifestEntry:
    return ManifestEntry(
        path=path,
        content_digest=hashlib.sha256(payload).hexdigest(),
        parser_profile="source",
        mode=mode,
        size=len(payload),
    )


def test_review_range_from_wire_is_bounded_and_typed() -> None:
    parsed = review_range_from_wire(
        {
            "mode": "working_tree",
            "base_rev": "HEAD",
            "head_rev": "WORKDIR",
            "base_sha": "a" * 40,
            "head_sha": "",
            "merge_base_sha": "",
            "dirty": True,
            "title": "working tree",
        }
    )
    assert parsed.mode == "working_tree"
    assert parsed.base_sha == "a" * 40
    assert parsed.dirty is True

    with pytest.raises(ServerError) as caught:
        review_range_from_wire({"mode": "elsewhere"})
    assert caught.value.code is ErrorCode.PAYLOAD_INVALID


def test_server_derives_working_tree_packet_from_exact_view_bytes(tmp_path: Path) -> None:
    old = b"def alpha():\n    return 0\n"
    new = b"def alpha():\n    return 1\n"
    old_entry = _entry("pkg/alpha.py", old)
    new_entry = _entry("pkg/alpha.py", new, mode=0o100755)
    content = _Content({old_entry.content_digest: old, new_entry.content_digest: new})
    described = RevRange(
        mode="working_tree",
        base_rev="HEAD",
        head_rev="WORKDIR",
        base_sha="a" * 40,
        head_sha="",
        dirty=True,
        title="Hosted working tree",
    )

    build = derive_review_packet_from_views(
        org_id="org-a",
        repo_id="repo-a",
        content=content,  # type: ignore[arg-type]
        base_membership={old_entry.path: old_entry},
        head_membership={new_entry.path: new_entry},
        range_spec=described,
        scratch_root=tmp_path,
        with_impact=False,
    )

    packet = build.packet
    assert packet.repo_root == "hosted:org-a:repo-a"
    assert packet.range_mode == "working_tree"
    assert packet.base_sha == "a" * 40
    assert packet.head_sha == ""
    assert packet.title == "Hosted working tree"
    assert len(packet.files) == 1
    changed = packet.files[0]
    assert changed.path == "pkg/alpha.py"
    assert changed.status == "modified"
    assert changed.additions == 1
    assert changed.deletions == 1
    assert "-    return 0" in changed.hunks[0].patch
    assert "+    return 1" in changed.hunks[0].patch
    assert build.blobs.old["pkg/alpha.py"] == old.decode()
    assert build.blobs.new["pkg/alpha.py"] == new.decode()
    assert "provenance_disabled" in packet.degraded


def test_server_derivation_never_uses_client_revision_strings_as_git_arguments(tmp_path: Path) -> None:
    old = b"value = 1\n"
    new = b"value = 2\n"
    old_entry = _entry("value.py", old)
    new_entry = _entry("value.py", new)
    content = _Content({old_entry.content_digest: old, new_entry.content_digest: new})
    described = RevRange(
        mode="commit_range",
        base_rev="--upload-pack=evil",
        head_rev="$(touch nope)",
        base_sha="client-base",
        head_sha="client-head",
        dirty=False,
        title="commit range",
    )

    build = derive_review_packet_from_views(
        org_id="org-a",
        repo_id="repo-a",
        content=content,  # type: ignore[arg-type]
        base_membership={old_entry.path: old_entry},
        head_membership={new_entry.path: new_entry},
        range_spec=described,
        scratch_root=tmp_path,
        with_impact=False,
    )

    assert build.packet.base_rev == "--upload-pack=evil"
    assert build.packet.head_rev == "$(touch nope)"
    assert build.packet.base_sha == "client-base"
    assert build.packet.head_sha == "client-head"
    assert not (tmp_path / "nope").exists()


def test_server_derivation_preserves_gitlink_pointer_changes(tmp_path: Path) -> None:
    described = RevRange(
        mode="working_tree",
        base_rev="HEAD",
        head_rev="WORKDIR",
        base_sha="a" * 40,
        head_sha="",
        dirty=True,
        title="submodule bump",
    )
    old_oid = "1" * 40
    new_oid = "2" * 40

    build = derive_review_packet_from_views(
        org_id="org-a",
        repo_id="repo-a",
        content=_Content({}),  # type: ignore[arg-type]
        base_membership={},
        head_membership={},
        base_gitlinks={"deps/sub": old_oid},
        head_gitlinks={"deps/sub": new_oid},
        range_spec=described,
        scratch_root=tmp_path,
        with_impact=False,
    )

    assert len(build.packet.files) == 1
    changed = build.packet.files[0]
    assert changed.path == "deps/sub"
    assert changed.category == "config"
    assert changed.additions == changed.deletions == 0
    assert changed.hunks == ()
    assert changed.submodule_pointer == (old_oid, new_oid)
    assert build.packet.stats["files"] == 1


def test_server_derivation_names_the_changed_symbols_with_or_without_the_impact_pass(tmp_path: Path) -> None:
    old = b"def alpha():\n    return 0\n\n\ndef beta():\n    return 0\n"
    new = b"def alpha():\n    return 1\n\n\ndef beta():\n    return 0\n"
    old_entry, new_entry = _entry("pkg/alpha.py", old), _entry("pkg/alpha.py", new)
    content = _Content({old_entry.content_digest: old, new_entry.content_digest: new})
    described = RevRange(
        mode="working_tree", base_rev="HEAD", head_rev="WORKDIR", base_sha="a" * 40, head_sha="", dirty=True
    )

    def derive(name: str, *, with_impact: bool) -> ReviewPacket:
        return derive_review_packet_from_views(
            org_id="org-a",
            repo_id="repo-a",
            content=content,  # type: ignore[arg-type]
            base_membership={old_entry.path: old_entry},
            head_membership={new_entry.path: new_entry},
            range_spec=described,
            scratch_root=tmp_path / name,
            with_impact=with_impact,
        ).packet

    def targets(packet: ReviewPacket) -> list[tuple[str, str, str]]:
        return [(symbol.file_path, symbol.symbol_name, symbol.change) for symbol in packet.symbols]

    skipped, full = derive("skipped", with_impact=False), derive("full", with_impact=True)

    # The unchanged neighbour is no target; the edited function is, either way.
    assert targets(skipped) == [("pkg/alpha.py", "alpha", "modified")] == targets(full)
    assert skipped.stats["symbols"] == 1
    # Only the fan-out is skipped, and the packet still says so.
    assert skipped.impact == ()
    assert "impact_disabled" in skipped.degraded and "impact_disabled" not in full.degraded
