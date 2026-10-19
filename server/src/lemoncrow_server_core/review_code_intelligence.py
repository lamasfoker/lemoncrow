"""Native generation-aware code intelligence adapted for Review."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

from lemoncrow.core.service.code_relations import balanced_relationship_rows
from lemoncrow.pro.capabilities.review.code_intelligence import (
    CodeIntelligenceLayerReadiness,
    CodeIntelligenceReadiness,
    ReadinessState,
    unavailable_readiness,
)

from .index.contracts import IndexBackend, IndexLayerState, IndexLayerStatus, ViewState
from .index.deferred import enqueue_deferred_layer
from .index.query import IndexQuery, QueryAnswer


def _layer(status: IndexLayerStatus) -> CodeIntelligenceLayerReadiness:
    state: ReadinessState
    if status.state is IndexLayerState.READY:
        state = "ready"
    elif status.state is IndexLayerState.UNAVAILABLE:
        state = "unavailable"
    elif "content_incomplete" in status.reason:
        # This is not merely queued work: the layer cannot become complete from
        # the content this View may currently address. Preserve that native
        # reason as an explicit degraded state rather than calling it building.
        state = "degraded"
    else:
        state = "building"
    return CodeIntelligenceLayerReadiness(
        state,
        reason=status.reason or ("" if status.ready else status.state.value),
        native_state=status.state.value,
        covered_paths=status.covered_paths,
        at_view_revision=status.at_view_revision,
    )


def _node_id(path: str, line: int, label: str) -> str:
    digest = hashlib.sha256(f"{path}\0{line}\0{label}".encode()).hexdigest()[:16]
    return f"native:{digest}"


# Repository-wide link summaries keyed by (org, View, View revision). A few
# entries cover the Views one local server serves at once; older revisions are
# evicted first.
_REPOSITORY_LINKS: dict[tuple[str, str, int], dict[str, Any]] = {}


class NativeReviewCodeIntelligence:
    """Review adapter over an authorized native View.

    The resolver is intentionally dynamic. Hosted Reader apps are cached across
    requests/principals, while View possession is request-specific. Every query
    therefore resolves a View owned by the currently-bound subject and then
    delegates to IndexQuery, preserving its possession and candidate-first
    semantics.
    """

    __slots__ = (
        "_access_allowed",
        "_backend",
        "_content_size_cap",
        "_keep_view_leased",
        "_org_id",
        "_preferred_view_id",
        "_preferred_view_revision",
        "_queries",
        "_repo_id",
        "_restore_view",
        "_subject",
    )

    def __init__(
        self,
        *,
        backend: IndexBackend,
        queries: IndexQuery,
        org_id: str,
        repo_id: str,
        subject: Callable[[], str],
        content_size_cap: int,
        access_allowed: Callable[[], bool] | None = None,
        preferred_view_id: str = "",
        preferred_view_revision: int = 0,
        restore_view: Callable[[], tuple[str, int] | None] | None = None,
        keep_view_leased: Callable[[str], None] | None = None,
    ) -> None:
        """*restore_view* reopens the Snapshot's View from its stored source tree
        once the bound one lapsed; *keep_view_leased* keeps a View in use alive."""
        self._backend = backend
        self._queries = queries
        self._org_id = org_id
        self._repo_id = repo_id
        self._subject = subject
        self._content_size_cap = max(1, int(content_size_cap))
        self._access_allowed = access_allowed or (lambda: True)
        self._preferred_view_id = preferred_view_id.strip()
        self._preferred_view_revision = max(0, int(preferred_view_revision))
        self._restore_view = restore_view
        self._keep_view_leased = keep_view_leased

    def _resolve_view(self) -> tuple[tuple[IndexBackend, ViewState] | None, str]:
        if not self._access_allowed():
            return None, "content_read_not_allowed"
        subject = self._subject()
        if not subject:
            return None, "no_bound_subject"
        try:
            view_ids = tuple(self._backend.views.view_ids_for_user(self._org_id, subject))
        except Exception:
            return None, "native_view_lookup_failed"

        if self._preferred_view_id:
            state = self._preferred_state(view_ids)
            if state is None and self._restored():
                # The Snapshot's View lapsed with its lease; its exact source
                # tree is stored with the Review, so it was opened again.
                state = self._preferred_state(None)
            return self._bound(state)

        candidates: list[tuple[IndexBackend, ViewState]] = []
        for view_id in view_ids:
            try:
                state = self._backend.views.get(self._org_id, view_id)
                if state.repo_id != self._repo_id:
                    continue
                candidates.append((self._backend.for_view(self._org_id, view_id), state))
            except Exception:
                continue
        if len(candidates) == 1:
            return candidates[0], ""
        # Old Review revisions have no persisted source View binding. Picking the
        # newest lease here can silently cross worktrees, so ambiguity -- or no
        # View at all -- opens the revision's own stored tree instead, exactly.
        if self._restored():
            return self._bound(self._preferred_state(None))
        return None, ("ambiguous_authorized_native_views" if candidates else "no_authorized_native_view")

    def _restored(self) -> bool:
        """Reopen the revision's View from its stored tree; bind to it when it opens."""
        restored = self._restore_view() if self._restore_view is not None else None
        if restored is None:
            return False
        self._preferred_view_id, self._preferred_view_revision = restored[0], max(0, int(restored[1]))
        return True

    def _bound(self, state: ViewState | None) -> tuple[tuple[IndexBackend, ViewState] | None, str]:
        if state is None or state.repo_id != self._repo_id:
            return None, "bound_native_view_unavailable"
        try:
            if self._keep_view_leased is not None:
                self._keep_view_leased(self._preferred_view_id)
            return (self._backend.for_view(self._org_id, self._preferred_view_id), state), ""
        except Exception:
            return None, "bound_native_view_unavailable"

    def _preferred_state(self, owned: tuple[str, ...] | None) -> ViewState | None:
        """The bound View while it is live and, when *owned* is given, among them."""
        if owned is not None and self._preferred_view_id not in owned:
            return None
        try:
            return self._backend.views.get(self._org_id, self._preferred_view_id)
        except Exception:
            return None

    def _view(self) -> tuple[IndexBackend, ViewState] | None:
        resolved, _reason = self._resolve_view()
        return resolved

    def _source_layer_readiness(
        self, leaf: IndexBackend, state: ViewState
    ) -> tuple[CodeIntelligenceLayerReadiness, CodeIntelligenceLayerReadiness, tuple[str, ...]]:
        ready = CodeIntelligenceLayerReadiness(
            "ready",
            covered_paths=state.entry_count,
            at_view_revision=state.view_revision,
        )
        reachability = leaf.view_content_reachability
        if reachability is None:
            return ready, ready, ()
        try:
            report = reachability(self._org_id, state.view_id, self._content_size_cap)
        except Exception:
            degraded = CodeIntelligenceLayerReadiness(
                "degraded",
                reason="content_reachability_unavailable",
                native_state="unknown",
                at_view_revision=state.view_revision,
            )
            return (
                degraded,
                degraded,
                ("lexical:content_reachability_unavailable", "structural:content_reachability_unavailable"),
            )
        if report.missing_paths <= 0:
            return ready, ready, ()
        covered = max(0, report.required_paths - report.missing_paths)
        degraded = CodeIntelligenceLayerReadiness(
            "degraded",
            reason="content_incomplete",
            native_state="content_incomplete",
            covered_paths=covered,
            at_view_revision=state.view_revision,
        )
        return degraded, degraded, ("lexical:content_incomplete", "structural:content_incomplete")

    def readiness(self) -> CodeIntelligenceReadiness:
        resolved, unavailable_reason = self._resolve_view()
        if resolved is None:
            return unavailable_readiness(unavailable_reason or "no_authorized_native_view", backend="native")
        leaf, state = resolved
        if not state.manifest_complete:
            building = CodeIntelligenceLayerReadiness(
                "building",
                reason="manifest_incomplete",
                native_state="building",
                at_view_revision=state.view_revision,
            )
            return CodeIntelligenceReadiness(
                backend="native",
                lexical=building,
                structural=building,
                graph=_layer(leaf.links.status(self._org_id, state.view_id)),
                semantic=_layer(leaf.semantic.status(self._org_id, state.view_id)),
                view_id=state.view_id,
                view_revision=state.view_revision,
                generation_id=state.generation_id,
                base_revision=state.base_revision,
                manifest_root=state.manifest_root,
                reason="manifest_incomplete",
            )
        lexical, structural, source_degraded = self._source_layer_readiness(leaf, state)
        try:
            generation = leaf.views.generation(self._org_id, state.view_id)
            generation_id = generation.generation_id
            base_revision = generation.base_revision
            manifest_root = generation.manifest_root
        except Exception:
            generation_id = state.generation_id
            base_revision = state.base_revision
            manifest_root = state.manifest_root
        graph = _layer(leaf.links.status(self._org_id, state.view_id))
        semantic = _layer(leaf.semantic.status(self._org_id, state.view_id))
        degraded = (
            *source_degraded,
            *(
                reason
                for reason in (
                    f"graph:{graph.reason}" if graph.state != "ready" and graph.reason else "",
                    f"semantic:{semantic.reason}" if semantic.state != "ready" and semantic.reason else "",
                )
                if reason
            ),
        )
        return CodeIntelligenceReadiness(
            backend="native",
            lexical=lexical,
            structural=structural,
            graph=graph,
            semantic=semantic,
            view_id=state.view_id,
            view_revision=state.view_revision,
            generation_id=generation_id,
            base_revision=base_revision,
            manifest_root=manifest_root,
            degraded_reasons=degraded,
        )

    def _run(
        self,
        *,
        kind: str,
        query: str = "",
        path: str = "",
        symbol: str = "",
        limit: int,
        outgoing: bool = False,
    ) -> QueryAnswer | None:
        resolved = self._view()
        if resolved is None:
            return None
        _leaf, state = resolved
        return self._queries.run(
            org_id=self._org_id,
            view_id=state.view_id,
            kind=kind,
            query=query,
            path=path,
            symbol=symbol,
            limit=limit,
            outgoing=outgoing,
        )

    def find_symbol(
        self,
        symbol_name: str,
        *,
        path: str = "",
        qualified_name: str = "",
        start_line: int = 0,
        limit: int = 12,
    ) -> Mapping[str, Any] | None:
        answer = self._run(kind="symbol", symbol=symbol_name, limit=max(1, min(limit, 32)))
        if answer is None:
            return None
        source_revision_exact = bool(self._preferred_view_id) and (
            answer.at_view_revision == self._preferred_view_revision
        )
        candidates: list[dict[str, Any]] = []
        for hit in answer.hits:
            if path and hit.path != path:
                continue
            definitions = hit.detail.get("definitions")
            if not isinstance(definitions, list):
                continue
            for raw in definitions:
                if not isinstance(raw, Mapping) or str(raw.get("name") or "") != symbol_name:
                    continue
                line = int(raw.get("line") or 0)
                candidates.append(
                    {
                        "id": _node_id(hit.path, line, symbol_name),
                        "path": hit.path,
                        "label": symbol_name,
                        # Native Layer-1 currently proves only the bare definition
                        # name. Preserve a Snapshot qualified name only while this
                        # query is still against the exact captured View revision.
                        "qualified_name": qualified_name if source_revision_exact and qualified_name else symbol_name,
                        "kind": str(raw.get("kind") or "symbol"),
                        "line": line,
                        # SymbolDef.start/end are byte offsets of the definition
                        # token, not source line geometry. Do not turn them into a
                        # fake function span and accidentally attribute another
                        # function's outgoing calls to this symbol.
                        "end_line": line,
                    }
                )
        # Snapshot line geometry is an identity aid only while the bound native
        # View is still at the exact revision captured with the Review. Once that
        # View advances, line numbers may now name a different same-name symbol.
        if start_line > 0 and source_revision_exact:
            exact_line = [item for item in candidates if int(item["line"]) == start_line]
            if len(exact_line) == 1:
                return exact_line[0]
        if len(candidates) != 1:
            return None
        if qualified_name and qualified_name != symbol_name and not source_revision_exact:
            # The native index cannot independently prove qualified identity yet.
            # Returning a unique bare-name match here could relabel A.run as B.run
            # after the worktree advanced, so degrade instead of guessing.
            return None
        return candidates[0]

    def neighborhood(
        self,
        symbol: Mapping[str, Any],
        *,
        depth: int = 1,
        limit: int = 40,
    ) -> Mapping[str, Any]:
        del depth  # Native graph query is one bounded surrounding hop.
        path = str(symbol.get("path") or "")
        label = str(symbol.get("label") or symbol.get("qualified_name") or "")
        line = int(symbol.get("line") or 0)
        end_line = int(symbol.get("end_line") or line)
        focus_id = str(symbol.get("id") or _node_id(path, line, label))
        bounded = max(1, min(limit, 40))
        # Callers are the edges that land on this symbol's name; asking for
        # exactly those keeps a busy file's other edges from crowding them out.
        answer = self._run(kind="relations", path=path, symbol=label, limit=bounded)
        if answer is None:
            return {"nodes": [], "edges": [], "degraded": True, "degraded_reason": "index_unavailable"}
        nodes: dict[str, dict[str, Any]] = {
            focus_id: {
                "id": focus_id,
                "path": path,
                "label": label,
                "qualified_name": str(symbol.get("qualified_name") or label),
                "kind": str(symbol.get("kind") or "symbol"),
                "line": line,
                "end_line": end_line,
            }
        }
        edges: list[dict[str, Any]] = []
        for hit in answer.hits:
            detail = hit.detail
            src = str(detail.get("src_path") or "")
            dst = str(detail.get("dst_path") or "")
            edge_symbol = str(detail.get("symbol") or "")
            relation_kind = str(detail.get("kind") or "reference")
            src_line = int(detail.get("src_line") or 0)
            # Native Edge identifies the target symbol exactly but does not yet
            # identify the source/caller symbol span. Inbound call/reference
            # edges are therefore attribution-safe; path-wide outgoing edges are
            # not and must not be attached to this focused symbol.
            if relation_kind not in {"call", "reference"}:
                continue
            if dst == path and (not label or edge_symbol == label):
                caller_id = _node_id(src, src_line, edge_symbol or src)
                nodes.setdefault(
                    caller_id,
                    {
                        "id": caller_id,
                        "path": src,
                        "label": edge_symbol or src,
                        "qualified_name": "",
                        "kind": "caller" if relation_kind == "call" else "reference",
                        "line": src_line,
                        "end_line": src_line,
                    },
                )
                edges.append({"source": caller_id, "target": focus_id, "kind": relation_kind, "weight": 1})
        truncated = answer.truncated

        # Outgoing edges carry only a source line, so they can be attributed to
        # this symbol only through a body span the caller vouches for (the
        # Review Snapshot's), and only while the View is still at the revision
        # that span was captured from. Otherwise the line may now sit in a
        # different function, and the callees are left out rather than guessed.
        span_start = int(symbol.get("span_start") or 0)
        span_end = int(symbol.get("span_end") or 0)
        exact_revision = bool(self._preferred_view_id) and answer.at_view_revision == self._preferred_view_revision
        outgoing_attributed = exact_revision and 0 < span_start < span_end
        if outgoing_attributed:
            # Only the edges leaving the file: a busy file's inbound edges must
            # not use up the budget and leave the symbol showing no calls.
            outgoing = self._run(kind="relations", path=path, outgoing=True, limit=200)
            callees: dict[str, dict[str, Any]] = {}
            for hit in outgoing.hits if outgoing is not None else ():
                detail = hit.detail
                relation_kind = str(detail.get("kind") or "reference")
                src_line = int(detail.get("src_line") or 0)
                dst = str(detail.get("dst_path") or "")
                callee = str(detail.get("symbol") or "")
                if relation_kind not in {"call", "reference"} or not dst or not callee:
                    continue
                if str(detail.get("src_path") or "") != path or not span_start <= src_line <= span_end:
                    continue
                if dst == path and callee == label:
                    continue  # Recursion says nothing about what the symbol depends on.
                callee_id = _node_id(dst, 0, callee)
                row = callees.get(callee_id)
                if row is None:
                    row = {"source": focus_id, "target": callee_id, "kind": relation_kind, "weight": 0}
                    callees[callee_id] = row
                    nodes.setdefault(
                        callee_id,
                        {
                            "id": callee_id,
                            "path": dst,
                            "label": callee,
                            "qualified_name": "",
                            "kind": "callee",
                            "line": 0,
                            "end_line": 0,
                        },
                    )
                if relation_kind == "call":
                    row["kind"] = "call"
                row["weight"] += 1
            edges.extend(callees.values())
            truncated = truncated or bool(outgoing is not None and outgoing.truncated)
        return {
            "nodes": list(nodes.values())[:80],
            "edges": edges[:80],
            "degraded": answer.degraded,
            "degraded_reason": answer.degraded_reason,
            "truncated": truncated,
            "outgoing_attributed": outgoing_attributed,
        }

    def file_relationships(
        self,
        paths: tuple[str, ...],
        *,
        limit: int = 320,
    ) -> Mapping[str, Any]:
        resolved = self._view()
        if resolved is None:
            return {
                "edges": [],
                "degraded": True,
                "degraded_reason": "index_unavailable",
                "truncated": False,
            }

        leaf, state = resolved
        # As many as the Review Map overview shows changed files (300).
        bounded_paths = tuple(dict.fromkeys(path for path in paths if path))[:300]
        bounded_limit = max(1, min(int(limit), 640))
        status = leaf.links.status(self._org_id, state.view_id)
        # The durable generation graph can already be present after process
        # restart while the in-memory readiness status is still cold. Read the
        # exact View/generation graph first; if it has path edges, they are usable
        # immediately and there is nothing to make Review wait for.
        raw_edges = leaf.links.edges_for_paths(self._org_id, state.view_id, bounded_paths)
        if not raw_edges and (not status.ready or status.at_view_revision != state.view_revision):
            # Review Map is an explicit graph consumer. A genuinely cold/deferred
            # View can exist without another query requesting Layer 3, so schedule
            # the missing work and let the UI re-read readiness shortly.
            enqueue_deferred_layer(leaf, self._org_id, state.view_id, "link")
            return {
                "edges": [],
                "degraded": True,
                "degraded_reason": f"link:{status.reason or status.state.value}",
                "truncated": False,
            }

        seeds = set(bounded_paths)
        aggregate: dict[tuple[str, str, str], dict[str, Any]] = {}
        kind_name = {"call": "calls", "reference": "references", "import": "imports"}
        for edge in raw_edges:
            relation = kind_name.get(edge.kind.value, edge.kind.value)
            key = (edge.src_path, edge.dst_path, relation)
            row = aggregate.get(key)
            if row is None:
                row = {
                    "source_path": edge.src_path,
                    "target_path": edge.dst_path,
                    "kind": relation,
                    "weight": 0,
                    "evidence": [],
                    "symbols": {},
                }
                aggregate[key] = row
            row["weight"] += 1
            if edge.symbol:
                # Every name the relationship lands on, not just the first few
                # evidence sites: the Review Map attaches incoming calls to the
                # changed symbol they reach.
                named = row["symbols"]
                named[edge.symbol] = int(named.get(edge.symbol, 0)) + 1
            evidence = row["evidence"]
            if len(evidence) < 4:
                item = {
                    "kind": relation,
                    "path": edge.src_path,
                    "line": edge.src_line,
                    "symbol": edge.symbol,
                }
                if item not in evidence:
                    evidence.append(item)

        rows = list(aggregate.values())
        for row in rows:
            named = row["symbols"]
            row["symbols"] = [
                {"name": name, "count": count}
                for name, count in sorted(named.items(), key=lambda item: (-item[1], item[0]))[:12]
            ]
        return {
            "edges": balanced_relationship_rows(rows, seeds, bounded_limit),
            "degraded": False,
            "degraded_reason": "",
            "truncated": len(rows) > bounded_limit,
            "at_view_revision": state.view_revision,
        }

    def repository_file_links(self) -> Mapping[str, Any]:
        """Every file-to-file link in the View, aggregated by (source, target, kind).

        The `/code` module overview folds these into module edges. The result is
        cached per View revision: the link layer only changes when the View does.
        """

        resolved = self._view()
        if resolved is None:
            return {"edges": [], "degraded": True, "degraded_reason": "index_unavailable", "truncated": False}
        leaf, state = resolved
        key = (self._org_id, state.view_id, state.view_revision)
        cached = _REPOSITORY_LINKS.get(key)
        if cached is not None:
            return cached
        status = leaf.links.status(self._org_id, state.view_id)
        raw_edges = leaf.links.edges(self._org_id, state.view_id)
        if not raw_edges and (not status.ready or status.at_view_revision != state.view_revision):
            enqueue_deferred_layer(leaf, self._org_id, state.view_id, "link")
            return {
                "edges": [],
                "degraded": True,
                "degraded_reason": f"link:{status.reason or status.state.value}",
                "truncated": False,
            }
        kind_name = {"call": "calls", "reference": "references", "import": "imports"}
        aggregate: dict[tuple[str, str, str], dict[str, Any]] = {}
        for edge in raw_edges:
            if edge.src_path == edge.dst_path:
                continue
            relation = kind_name.get(edge.kind.value, edge.kind.value)
            row_key = (edge.src_path, edge.dst_path, relation)
            row = aggregate.get(row_key)
            if row is None:
                row = {
                    "source_path": edge.src_path,
                    "target_path": edge.dst_path,
                    "kind": relation,
                    "weight": 0,
                    "symbols": {},
                }
                aggregate[row_key] = row
            row["weight"] += 1
            if edge.symbol:
                named = row["symbols"]
                named[edge.symbol] = int(named.get(edge.symbol, 0)) + 1
        rows = list(aggregate.values())
        for row in rows:
            named = row["symbols"]
            row["symbols"] = [
                {"name": name, "count": count}
                for name, count in sorted(named.items(), key=lambda item: (-item[1], item[0]))[:4]
            ]
        payload = {
            "edges": rows,
            "degraded": False,
            "degraded_reason": "",
            "truncated": False,
            "at_view_revision": state.view_revision,
        }
        try:
            while len(_REPOSITORY_LINKS) >= 4:
                _REPOSITORY_LINKS.pop(next(iter(_REPOSITORY_LINKS)), None)
        except (RuntimeError, StopIteration):
            pass  # Another request evicted concurrently; the cap still holds.
        _REPOSITORY_LINKS[key] = payload
        return payload

    def path_digest(self, path: str) -> str:
        """Content digest the View holds for *path*, or ``""``.

        `/code` compares it with the file on disk before trusting line geometry
        from another index against this View's link lines.
        """

        resolved = self._view()
        if resolved is None:
            return ""
        leaf, state = resolved
        try:
            entry = leaf.views.entry(self._org_id, state.view_id, path)
        except Exception:
            return ""
        return str(entry.content_digest) if entry is not None else ""

    def references(self, symbol: Mapping[str, Any], *, limit: int = 20) -> Mapping[str, Any]:
        label = str(symbol.get("label") or symbol.get("qualified_name") or "")
        path = str(symbol.get("path") or "")
        answer = self._run(kind="relations", symbol=label, limit=max(1, min(limit, 20)))
        if answer is None:
            return {"references": [], "degraded": True, "degraded_reason": "index_unavailable"}
        refs: list[dict[str, Any]] = []
        for hit in answer.hits:
            detail = hit.detail
            if str(detail.get("dst_path") or "") != path:
                continue
            if label and str(detail.get("symbol") or "") != label:
                continue
            relation_kind = str(detail.get("kind") or "reference")
            if relation_kind not in {"call", "reference"}:
                continue
            line = int(detail.get("src_line") or 0)
            refs.append(
                {
                    "path": str(detail.get("src_path") or hit.path),
                    "line": line,
                    "end_line": line,
                    "caller": "",
                    "edge_kind": relation_kind,
                }
            )
            if len(refs) >= limit:
                break
        return {
            "references": refs,
            "degraded": answer.degraded,
            "degraded_reason": answer.degraded_reason,
            "truncated": answer.truncated,
        }

    def semantic_search(self, query: str, *, limit: int = 8) -> Mapping[str, Any]:
        bounded = max(1, min(limit, 12))
        answer = self._run(kind="semantic", query=query, limit=bounded)
        if answer is None:
            return {
                "hits": [],
                "degraded": True,
                "degraded_reason": "index_unavailable",
                "truncated": False,
            }
        return {
            "hits": [hit.to_wire() for hit in answer.hits[:bounded]],
            "degraded": answer.degraded,
            "degraded_reason": answer.degraded_reason,
            "truncated": answer.truncated,
            "view_revision": answer.at_view_revision,
        }

    def ranking_metadata(self, symbol: Mapping[str, Any]) -> Mapping[str, Any]:
        del symbol
        # The native substrate does not currently expose an attribution-safe
        # centrality projection through IndexQuery. Absence is explicit; Review
        # must not synthesize a confidence/rank from another signal.
        return {}
