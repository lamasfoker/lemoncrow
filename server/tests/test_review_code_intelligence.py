from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from lemoncrow_server_core.index.contracts import ManifestEntry, RepoIdentityClaim, sha256_hex
from lemoncrow_server_core.index.manifest import chunk_manifest, manifest_root
from lemoncrow_server_core.index.query import IndexQuery
from lemoncrow_server_core.index.repositories import RepositoryIndex
from lemoncrow_server_core.review_code_intelligence import NativeReviewCodeIntelligence, balanced_relationship_rows

from lemoncrow.pro.capabilities.review.review_map import _stable_id, build_review_map

CAP = 1024**2
ORG = "org_review_intelligence"


def _open(
    owner: RepositoryIndex,
    *,
    subject: str = "alice",
    files: dict[str, bytes] | None = None,
    base_revision: str = "h" * 40,
    lease_s: float = 100_000,
) -> tuple[str, str]:
    files = files or {
        "a.py": b"from b import beta\n\ndef alpha():\n    return beta()\n",
        "b.py": b"def beta():\n    return 1\n",
    }
    repo = owner.backend.repos.canonical_repo_id(ORG, RepoIdentityClaim("", "", "review-intelligence"))
    entries = tuple(
        ManifestEntry(path, sha256_hex(data), "python-1", 0o100644, len(data)) for path, data in sorted(files.items())
    )
    chunks = chunk_manifest(entries)
    opened = owner.backend.views.open(
        org_id=ORG,
        repo_id=repo,
        base_revision=base_revision,
        manifest_root=manifest_root(entries),
        chunk_ids=tuple(chunk.chunk_id for chunk in chunks),
        lease_s=lease_s,
        content_subject=subject,
        owner_subject=subject,
    )
    view_id = opened.state.view_id
    for chunk in chunks:
        owner.backend.views.put_manifest_chunk(
            org_id=ORG, view_id=view_id, chunk_id=chunk.chunk_id, entries=chunk.entries
        )
    leaf = owner.backend.for_view(ORG, view_id)
    for data in files.values():
        digest = sha256_hex(data)
        leaf.content.put(ORG, digest, data)
        leaf.analysis.ensure(ORG, digest, "python-1", data)
        leaf.provenance.record(ORG, repo, subject, (digest,))
    return repo, view_id


def _provider(
    owner: RepositoryIndex,
    repo: str,
    subject: str = "alice",
    *,
    preferred_view_id: str = "",
    preferred_view_revision: int = 0,
) -> NativeReviewCodeIntelligence:
    return NativeReviewCodeIntelligence(
        backend=owner.backend,
        queries=IndexQuery(owner.backend, content_size_cap=CAP),
        org_id=ORG,
        repo_id=repo,
        subject=lambda: subject,
        content_size_cap=CAP,
        preferred_view_id=preferred_view_id,
        preferred_view_revision=preferred_view_revision,
    )


def test_native_review_provider_uses_generation_view_and_bounded_graph(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        repo, view_id = _open(owner)
        provider = _provider(owner, repo)

        before = provider.readiness()
        assert before.backend == "native"
        assert before.view_id == view_id
        assert before.generation_id.startswith("gen_")
        assert before.graph.state == "building"

        beta = provider.find_symbol("beta", path="b.py")
        assert beta is not None
        neighborhood = provider.neighborhood(beta, depth=9, limit=999)
        assert len(neighborhood["nodes"]) <= 40
        assert len(neighborhood["edges"]) <= 40
        assert any(node["path"] == "a.py" for node in neighborhood["nodes"])

        refs = provider.references(beta, limit=999)
        assert len(refs["references"]) <= 20
        assert any(row["path"] == "a.py" for row in refs["references"])

        file_graph = provider.file_relationships(("a.py", "b.py"), limit=999)
        assert file_graph["degraded"] is False
        assert len(file_graph["edges"]) <= 640
        assert any(
            row["source_path"] == "a.py"
            and row["target_path"] == "b.py"
            and row["kind"] in {"calls", "imports", "references"}
            for row in file_graph["edges"]
        )

        semantic = provider.semantic_search("beta caller", limit=999)
        assert len(semantic["hits"]) <= 12
        assert semantic["view_revision"] == 0
        assert all(str(hit["path"]) in {"a.py", "b.py"} for hit in semantic["hits"])

        after = provider.readiness()
        assert after.graph.state == "ready"
        assert after.view_revision == 0
    finally:
        owner.close()


def test_file_relationship_budget_is_shared_between_changed_and_surrounding_links() -> None:
    def row(source: str, target: str, kind: str = "calls", weight: int = 5) -> dict[str, object]:
        return {"source_path": source, "target_path": target, "kind": kind, "weight": weight}

    seeds = {f"c{index}.py" for index in range(10)}
    internal = [row(source, target) for source in sorted(seeds) for target in sorted(seeds) if source != target]
    external = [row("cli.py", "c0.py", "imports", 1), row("c1.py", "util.py", "references", 1)]

    picked = balanced_relationship_rows(internal + external, seeds, 10)
    assert len(picked) == 10
    assert {(item["source_path"], item["target_path"]) for item in picked[-2:]} == {
        ("cli.py", "c0.py"),
        ("c1.py", "util.py"),
    }
    # Unused surrounding capacity is lent back to links among the changed files.
    assert sum(1 for item in picked if item in internal) == 8
    # And the other way round: no changed-file links leave room for more context.
    many_external = [row(f"x{index}.py", "c0.py") for index in range(20)]
    assert len(balanced_relationship_rows(internal[:2] + many_external, seeds, 10)) == 10
    assert (
        sum(1 for item in balanced_relationship_rows(internal[:2] + many_external, seeds, 10) if item in internal) == 2
    )


def test_native_review_provider_fails_closed_without_owned_view(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        repo, _view_id = _open(owner, subject="alice")
        provider = _provider(owner, repo, subject="mallory")
        readiness = provider.readiness()
        assert not readiness.available
        assert readiness.reason == "no_authorized_native_view"
        assert provider.find_symbol("beta", path="b.py") is None
    finally:
        owner.close()


def test_native_review_provider_binds_to_snapshot_source_view_across_worktrees(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        repo, review_view = _open(
            owner,
            files={"reviewed.py": b"def reviewed_symbol():\n    return 1\n"},
            base_revision="a" * 40,
            lease_s=1_000,
        )
        same_repo, other_view = _open(
            owner,
            files={"other.py": b"def other_worktree_symbol():\n    return 2\n"},
            base_revision="b" * 40,
            lease_s=2_000,
        )
        assert same_repo == repo
        assert other_view != review_view

        ambiguous = _provider(owner, repo)
        assert ambiguous.readiness().reason == "ambiguous_authorized_native_views"

        provider = _provider(owner, repo, preferred_view_id=review_view)
        readiness = provider.readiness()
        assert readiness.view_id == review_view
        assert readiness.base_revision == "a" * 40
        assert provider.find_symbol("reviewed_symbol", path="reviewed.py", start_line=1) is not None
        assert provider.find_symbol("other_worktree_symbol", path="other.py", start_line=1) is None
    finally:
        owner.close()


def test_native_symbol_matching_refuses_ambiguous_same_name_without_exact_line(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        source = b"class A:\n    def run(self):\n        return 1\n\nclass B:\n    def run(self):\n        return 2\n"
        repo, view_id = _open(owner, files={"a.py": source})
        provider = _provider(owner, repo, preferred_view_id=view_id)

        assert provider.find_symbol("run", path="a.py", qualified_name="B.run") is None
        second = provider.find_symbol("run", path="a.py", qualified_name="B.run", start_line=6)
        assert second is not None
        assert second["line"] == 6
        assert second["end_line"] == 6
    finally:
        owner.close()


def test_native_symbol_matching_does_not_reuse_snapshot_line_after_bound_view_advances(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        original = b"class A:\n    def run(self):\n        return 1\n\nclass B:\n    def run(self):\n        return 2\n"
        repo, view_id = _open(owner, files={"a.py": original})
        provider = _provider(
            owner,
            repo,
            preferred_view_id=view_id,
            preferred_view_revision=0,
        )
        before = provider.find_symbol("run", path="a.py", qualified_name="B.run", start_line=6)
        assert before is not None
        assert before["qualified_name"] == "B.run"

        advanced = b"class B:\n    def run(self):\n        return 2\n\nclass A:\n    def run(self):\n        return 1\n"
        digest = sha256_hex(advanced)
        leaf = owner.backend.for_view(ORG, view_id)
        leaf.content.put(ORG, digest, advanced)
        leaf.analysis.ensure(ORG, digest, "python-1", advanced)
        leaf.provenance.record(ORG, repo, "alice", (digest,))
        state = owner.backend.views.apply_overlay(
            org_id=ORG,
            view_id=view_id,
            expected_view_revision=0,
            client_seq=1,
            entries=(ManifestEntry("a.py", digest, "python-1", 0o100644, len(advanced)),),
            removed_paths=(),
        )
        assert state.view_revision == 1

        # Snapshot line 6 now names A.run. Native Layer-1 cannot prove the
        # qualified identity B.run independently, so current-index context must
        # degrade instead of relabeling A.run as B.run.
        assert provider.find_symbol("run", path="a.py", qualified_name="B.run", start_line=6) is None
    finally:
        owner.close()


def test_native_neighborhood_does_not_attribute_other_function_outgoing_calls(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        repo, view_id = _open(
            owner,
            files={
                "a.py": b"def target():\n    return 1\n\ndef unrelated():\n    return helper()\n",
                "b.py": b"def helper():\n    return 2\n",
            },
        )
        provider = _provider(owner, repo, preferred_view_id=view_id)
        target = provider.find_symbol("target", path="a.py", start_line=1)
        assert target is not None
        assert target["end_line"] == 1
        neighborhood = provider.neighborhood(target)
        assert neighborhood["edges"] == []
        assert all(node["path"] != "b.py" for node in neighborhood["nodes"])
    finally:
        owner.close()


def test_native_neighborhood_preserves_call_and_reference_relation_kinds(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        repo, view_id = _open(
            owner,
            files={
                "a.py": b"from b import beta\n\ndef alpha():\n    x = beta\n    return beta()\n",
                "b.py": b"def beta():\n    return 1\n",
            },
        )
        provider = _provider(owner, repo, preferred_view_id=view_id)
        beta = provider.find_symbol("beta", path="b.py", start_line=1)
        assert beta is not None
        neighborhood = provider.neighborhood(beta)
        kinds = {str(edge["kind"]) for edge in neighborhood["edges"]}
        assert "call" in kinds
        assert "reference" in kinds
        assert "import" not in kinds
    finally:
        owner.close()


def test_review_map_keeps_native_current_index_non_snapshot_exact_without_checkout(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        repo, _view_id = _open(owner)
        provider = _provider(owner, repo)
        symbol = {
            "symbol_name": "beta",
            "qualified_name": "beta",
            "kind": "function",
            "file_path": "b.py",
            "start_line": 1,
            "end_line": 2,
            "change": "modified",
            "caller_count": -1,
            "usage_count": -1,
        }
        packet = {
            "files": [{"path": "b.py", "status": "modified", "category": "code"}],
            "symbols": [symbol],
            "impact": [],
            "degraded": [],
        }
        node_id = _stable_id("symbol", "b.py", "beta", 1, "modified")
        revision = SimpleNamespace(
            id="rev-1",
            revision_number=1,
            tree_fingerprint="tree",
            source_fingerprint="source",
            base_sha="",
            head_sha="h" * 40,
            degraded=(),
        )
        payload = build_review_map(
            review_id="review-1",
            revision=revision,
            packet=packet,
            targets=(),
            evidence=(),
            context_repo_root=None,
            code_intelligence=provider,
            level="symbol",
            selected_node_id=node_id,
        )

        assert payload["snapshot"]["exactness"] == "snapshot"
        assert payload["context"]["source"] == "current_index"
        assert payload["context"]["exact_for_snapshot"] is False
        assert payload["context"]["generation_matches_snapshot_head"] is True
        assert payload["context"]["snapshot_head_matches_checkout"] is False
        assert payload["context"]["readiness"]["graph"]["state"] == "ready"
        focus = next(row for row in payload["graph"]["nodes"] if row["id"] == payload["graph"]["focus"])
        assert focus["exactness"] == "snapshot"
        current_nodes = [row for row in payload["graph"]["nodes"] if row["id"] != node_id]
        assert current_nodes
        assert all(row["exactness"] == "current_index" for row in current_nodes)
        current_edges = [edge for edge in payload["graph"]["edges"] if edge["exactness"] == "current_index"]
        assert {edge["kind"] for edge in current_edges} >= {"call", "reference"}
        assert all(edge["exactness"] in {"snapshot", "current_index"} for edge in payload["graph"]["edges"])
    finally:
        owner.close()


def test_review_map_reports_graph_deferred_without_losing_snapshot(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=1)
    try:
        repo, _view_id = _open(owner)
        provider = _provider(owner, repo)
        symbol = {
            "symbol_name": "beta",
            "qualified_name": "beta",
            "kind": "function",
            "file_path": "b.py",
            "start_line": 1,
            "end_line": 2,
            "change": "modified",
            "caller_count": -1,
            "usage_count": -1,
        }
        node_id = _stable_id("symbol", "b.py", "beta", 1, "modified")
        payload = build_review_map(
            review_id="review-deferred",
            revision=SimpleNamespace(
                id="rev-deferred",
                revision_number=1,
                tree_fingerprint="tree",
                source_fingerprint="source",
                base_sha="",
                head_sha="h" * 40,
                degraded=(),
            ),
            packet={
                "files": [{"path": "b.py", "status": "modified", "category": "code"}],
                "symbols": [symbol],
                "impact": [],
                "degraded": [],
            },
            targets=(),
            evidence=(),
            context_repo_root=None,
            code_intelligence=provider,
            level="symbol",
            selected_node_id=node_id,
        )
        assert payload["snapshot"]["exactness"] == "snapshot"
        assert payload["context"]["source"] == "current_index"
        assert payload["context"]["readiness"]["graph"]["state"] == "building"
        assert payload["context"]["readiness"]["graph"]["reason"] == "link_rebuild_deferred"
        assert payload["context"]["exact_for_snapshot"] is False
        assert "Graph intelligence is building" in payload["context"]["note"]
    finally:
        owner.close()


def test_review_map_stays_snapshot_usable_when_native_view_is_unavailable(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        repo, _view_id = _open(owner, subject="alice")
        provider = _provider(owner, repo, subject="mallory")
        symbol = {
            "symbol_name": "beta",
            "qualified_name": "beta",
            "kind": "function",
            "file_path": "b.py",
            "start_line": 1,
            "end_line": 2,
            "change": "modified",
            "caller_count": -1,
            "usage_count": -1,
        }
        payload = build_review_map(
            review_id="review-unavailable",
            revision=SimpleNamespace(
                id="rev-unavailable",
                revision_number=1,
                tree_fingerprint="tree",
                source_fingerprint="source",
                base_sha="",
                head_sha="h" * 40,
                degraded=(),
            ),
            packet={
                "files": [{"path": "b.py", "status": "modified", "category": "code"}],
                "symbols": [symbol],
                "impact": [],
                "degraded": [],
            },
            targets=(),
            evidence=(),
            context_repo_root=None,
            code_intelligence=provider,
            level="architecture",
        )
        assert payload["snapshot"]["exactness"] == "snapshot"
        assert payload["context"]["source"] == "unavailable"
        assert payload["context"]["exact_for_snapshot"] is False
        assert payload["context"]["readiness"]["reason"] == "no_authorized_native_view"
        assert payload["graph"]["nodes"]
    finally:
        owner.close()


def test_native_readiness_reports_content_incomplete_layers_as_degraded(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        files = {
            "a.py": b"from b import beta\n\ndef alpha():\n    return beta()\n",
            "b.py": b"def beta():\n    return 1\n",
        }
        repo = owner.backend.repos.canonical_repo_id(ORG, RepoIdentityClaim("", "", "review-degraded"))
        entries = tuple(
            ManifestEntry(path, sha256_hex(data), "python-1", 0o100644, len(data))
            for path, data in sorted(files.items())
        )
        chunks = chunk_manifest(entries)
        opened = owner.backend.views.open(
            org_id=ORG,
            repo_id=repo,
            base_revision="d" * 40,
            manifest_root=manifest_root(entries),
            chunk_ids=tuple(chunk.chunk_id for chunk in chunks),
            lease_s=100_000,
            content_subject="alice",
            owner_subject="alice",
        )
        view_id = opened.state.view_id
        for chunk in chunks:
            owner.backend.views.put_manifest_chunk(
                org_id=ORG, view_id=view_id, chunk_id=chunk.chunk_id, entries=chunk.entries
            )
        leaf = owner.backend.for_view(ORG, view_id)
        only = files["a.py"]
        digest = sha256_hex(only)
        leaf.content.put(ORG, digest, only)
        leaf.analysis.ensure(ORG, digest, "python-1", only)
        leaf.provenance.record(ORG, repo, "alice", (digest,))

        assert leaf.links.refresh(ORG, view_id, budget=100).reason == "link_content_incomplete"
        assert leaf.semantic.refresh(ORG, view_id, budget=100).reason == "semantic_engine_unavailable"

        readiness = _provider(owner, repo).readiness()
        assert readiness.lexical.state == "degraded"
        assert readiness.lexical.reason == "content_incomplete"
        assert readiness.structural.state == "degraded"
        assert readiness.structural.reason == "content_incomplete"
        assert readiness.graph.state == "degraded"
        assert readiness.graph.reason == "link_content_incomplete"
        # The public server has no semantic engine: unavailable, never "building".
        assert readiness.semantic.state == "unavailable"
        assert readiness.semantic.reason == "semantic_engine_unavailable"
        assert "graph:link_content_incomplete" in readiness.degraded_reasons
        assert "semantic:semantic_engine_unavailable" in readiness.degraded_reasons
    finally:
        owner.close()


def test_native_symbol_calls_are_attributed_only_inside_an_exact_snapshot_span(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        files = {
            "a.py": b"from b import beta\n\n\ndef alpha():\n    return beta()\n\n\ndef gamma():\n    return 2\n",
            "b.py": b"def beta():\n    return 1\n",
        }
        repo, view_id = _open(owner, files=files)
        exact = _provider(owner, repo, preferred_view_id=view_id, preferred_view_revision=0)
        alpha = exact.find_symbol("alpha", path="a.py")
        gamma = exact.find_symbol("gamma", path="a.py")
        assert alpha is not None and gamma is not None

        spanned = exact.neighborhood({**alpha, "span_start": 4, "span_end": 5})
        assert spanned["outgoing_attributed"] is True
        callees = [node for node in spanned["nodes"] if node["kind"] == "callee"]
        assert [(node["path"], node["label"]) for node in callees] == [("b.py", "beta")]
        assert any(
            edge["source"] == alpha["id"] and edge["target"] == callees[0]["id"] and edge["kind"] == "call"
            for edge in spanned["edges"]
        )

        # The same file's other function does not inherit alpha's call.
        other = exact.neighborhood({**gamma, "span_start": 8, "span_end": 9})
        assert other["outgoing_attributed"] is True
        assert not [node for node in other["nodes"] if node["kind"] == "callee"]

        # Without the Snapshot's View revision a source line proves nothing.
        unbound = _provider(owner, repo).neighborhood({**alpha, "span_start": 4, "span_end": 5})
        assert unbound["outgoing_attributed"] is False
        assert not [node for node in unbound["nodes"] if node["kind"] == "callee"]
    finally:
        owner.close()


def test_a_busy_files_inbound_edges_do_not_crowd_out_a_symbols_calls(tmp_path: Path) -> None:
    # The callee query read every edge touching the focused file under one
    # 200-row budget, so 210 callers into it left the symbol with no calls.
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=1000)
    try:
        callers = "from m import helper\n" + "".join(f"\n\ndef c{i}():\n    return helper()\n" for i in range(210))
        files = {
            "a.py": callers.encode(),
            "m.py": b"from z import zeta\n\n\ndef helper():\n    return 1\n\n\ndef alpha():\n    return zeta()\n",
            "z.py": b"def zeta():\n    return 0\n",
        }
        repo, view_id = _open(owner, files=files)
        exact = _provider(owner, repo, preferred_view_id=view_id, preferred_view_revision=0)
        alpha = exact.find_symbol("alpha", path="m.py")
        assert alpha is not None

        graph = exact.neighborhood({**alpha, "span_start": 8, "span_end": 9})
        callees = [(node["path"], node["label"]) for node in graph["nodes"] if node["kind"] == "callee"]
        assert callees == [("z.py", "zeta")]
        assert graph["truncated"] is False
    finally:
        owner.close()


def test_native_repository_links_aggregate_every_cross_file_link_and_expose_path_digests(tmp_path: Path) -> None:
    owner = RepositoryIndex(tmp_path / "indexes", content_size_cap=CAP, inline_rebuild_budget=100)
    try:
        repo, view_id = _open(owner)
        provider = _provider(owner, repo)
        # A relations query builds the View's link layer.
        beta = provider.find_symbol("beta", path="b.py")
        assert beta is not None
        provider.neighborhood(beta)

        links = provider.repository_file_links()
        assert links["degraded"] is False
        rows = {(row["source_path"], row["target_path"], row["kind"]): row for row in links["edges"]}
        assert ("a.py", "b.py", "calls") in rows
        assert rows[("a.py", "b.py", "calls")]["symbols"][0]["name"] == "beta"
        assert all(source != target for source, target, _kind in rows)
        # Cached per View revision: the same object comes back.
        assert provider.repository_file_links() is links

        assert provider.path_digest("b.py") == sha256_hex(b"def beta():\n    return 1\n")
        assert provider.path_digest("missing.py") == ""
        assert view_id
    finally:
        owner.close()
