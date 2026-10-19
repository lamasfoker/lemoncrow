"""Author guidance: a host-neutral, versioned handoff from the authoring agent.

Spec: docs/planning/2026-09-28-evidence-backed-review-design.md §6.

The author contributes intent, keyed change items, proposed human decisions,
concerns and assumptions. LemonCrow keeps that input attributed and versioned,
resolves its references against a Snapshot, and never lets it set a human
state. Capture follows ``rationale.py``: a sidecar bound to exact file content,
imported only after a ReviewRevision exists, so an agent can never move the
reviewer's selected Snapshot.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

from lemoncrow.core.foundation.paths import confine_to_root, session_dir
from lemoncrow.pro.capabilities.review.session_models import AuthorGuidanceRow
from lemoncrow.pro.capabilities.review.units import file_fingerprint

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession, ReviewUnit
    from lemoncrow.pro.capabilities.review.store import ReviewStore
    from lemoncrow.pro.capabilities.review.targets import ReviewTarget

GUIDANCE_SCHEMA_VERSION = 1

_KEY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_CHECK_KEY = re.compile(r"^browser:[A-Za-z0-9._-]{1,128}$")
_ATTENTION = frozenset({"", "review_first", "understand", "routine"})

_MAX_ITEMS = 64
_MAX_DECISIONS = 16
_MAX_TARGETS = 32
_MAX_LIST = 16
_MIN_OPTIONS = 2
_MAX_OPTIONS = 8


class GuidanceError(ValueError):
    """Guidance that cannot be accepted; the message names the field."""


@dataclass(frozen=True)
class GuidanceTargetRef:
    path: str
    symbol: str = ""
    start_line: int = 0
    end_line: int = 0
    content_hash: str = ""
    """Author-supplied content hash, kept as context; LemonCrow records its own."""


@dataclass(frozen=True)
class GuidanceAssumption:
    key: str
    value: str
    """The semantic value. A change of meaning must change this, not only ``statement``."""
    statement: str = ""
    scope: str = ""


@dataclass(frozen=True)
class GuidanceConcern:
    scope: str
    reason: str


@dataclass(frozen=True)
class GuidanceItem:
    key: str
    title: str
    targets: tuple[GuidanceTargetRef, ...]
    before: str = ""
    after: str = ""
    rationale: str = ""
    concerns: tuple[GuidanceConcern, ...] = ()
    assumptions: tuple[GuidanceAssumption, ...] = ()
    suggested_attention: str = ""
    evidence_refs: tuple[str, ...] = ()
    # Spec §3: the author states no external behavior is intended to change.
    internal_only: bool = False


@dataclass(frozen=True)
class GuidanceOption:
    key: str
    label: str


@dataclass(frozen=True)
class GuidanceDecision:
    key: str
    question: str
    options: tuple[GuidanceOption, ...]
    selected_option: str
    reason_human_required: str
    targets: tuple[GuidanceTargetRef, ...] = ()
    assumptions: tuple[GuidanceAssumption, ...] = ()
    contract_scope: tuple[str, ...] = ()
    relied_checks: tuple[str, ...] = ()


@dataclass(frozen=True)
class GuidancePayload:
    schema_version: int
    intent: str
    items: tuple[GuidanceItem, ...]
    human_decisions: tuple[GuidanceDecision, ...]
    suggested_order: tuple[str, ...] = ()
    routine_candidates: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# validation helpers
# --------------------------------------------------------------------------- #


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GuidanceError(f"{field} must be an object")
    if "confidence" in value:
        raise GuidanceError(
            f"{field}.confidence is not accepted: record concrete concerns and keyed assumptions instead of a number"
        )
    return value


def _list(value: object, field: str, limit: int) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list | tuple):
        raise GuidanceError(f"{field} must be a list")
    if len(value) > limit:
        raise GuidanceError(f"{field} has {len(value)} entries; at most {limit} are accepted")
    return list(value)


def _text(raw: Mapping[str, Any], name: str, field: str, limit: int, *, required: bool = False) -> str:
    value = raw.get(name)
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise GuidanceError(f"{field}.{name} must be text")
    text = value.strip()
    if required and not text:
        raise GuidanceError(f"{field}.{name} is required")
    if len(text) > limit:
        raise GuidanceError(f"{field}.{name} is longer than {limit} characters")
    return text


def _key(raw: Mapping[str, Any], field: str, name: str = "key") -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not _KEY.match(value):
        raise GuidanceError(f"{field}.{name} must match {_KEY.pattern} (stable within the Change)")
    return value


def _unique(keys: Sequence[str], field: str) -> None:
    seen: set[str] = set()
    for key in keys:
        if key in seen:
            raise GuidanceError(f"{field} has duplicate key {key!r}")
        seen.add(key)


def _int(raw: Mapping[str, Any], name: str, field: str) -> int:
    value = raw.get(name, 0)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GuidanceError(f"{field}.{name} must be a non-negative integer")
    return value


def _texts(value: object, field: str, limit: int, item_limit: int) -> tuple[str, ...]:
    out: list[str] = []
    for index, item in enumerate(_list(value, field, limit)):
        if not isinstance(item, str) or not item.strip():
            raise GuidanceError(f"{field}[{index}] must be non-empty text")
        if len(item.strip()) > item_limit:
            raise GuidanceError(f"{field}[{index}] is longer than {item_limit} characters")
        out.append(item.strip())
    return tuple(out)


def _target(raw: object, field: str) -> GuidanceTargetRef:
    data = _mapping(raw, field)
    path = _text(data, "path", field, 1024, required=True)
    posix = PurePosixPath(path.replace("\\", "/"))
    if posix.is_absolute() or ".." in posix.parts or path.startswith("~"):
        raise GuidanceError(f"{field}.path must be repository-relative and stay inside the repository")
    start = _int(data, "start_line", field)
    end = _int(data, "end_line", field)
    if start and end and end < start:
        raise GuidanceError(f"{field}.end_line is before start_line")
    if end and not start:
        raise GuidanceError(f"{field}.end_line needs a start_line")
    return GuidanceTargetRef(
        path=posix.as_posix(),
        symbol=_text(data, "symbol", field, 256),
        start_line=start,
        end_line=end or start,
        content_hash=_text(data, "content_hash", field, 128),
    )


def _targets(value: object, field: str) -> tuple[GuidanceTargetRef, ...]:
    return tuple(_target(item, f"{field}[{index}]") for index, item in enumerate(_list(value, field, _MAX_TARGETS)))


def _assumptions(value: object, field: str) -> tuple[GuidanceAssumption, ...]:
    out: list[GuidanceAssumption] = []
    for index, raw in enumerate(_list(value, field, _MAX_LIST)):
        where = f"{field}[{index}]"
        data = _mapping(raw, where)
        out.append(
            GuidanceAssumption(
                key=_key(data, where),
                value=_text(data, "value", where, 200, required=True),
                statement=_text(data, "statement", where, 600),
                scope=_text(data, "scope", where, 160),
            )
        )
    _unique([item.key for item in out], field)
    return tuple(out)


def _concerns(value: object, field: str) -> tuple[GuidanceConcern, ...]:
    out: list[GuidanceConcern] = []
    for index, raw in enumerate(_list(value, field, _MAX_LIST)):
        where = f"{field}[{index}]"
        data = _mapping(raw, where)
        out.append(
            GuidanceConcern(
                scope=_text(data, "scope", where, 160, required=True),
                reason=_text(data, "reason", where, 600, required=True),
            )
        )
    return tuple(out)


def _item(raw: object, field: str) -> GuidanceItem:
    data = _mapping(raw, field)
    behavior_raw = data.get("behavior")
    behavior = _mapping(behavior_raw, f"{field}.behavior") if behavior_raw is not None else data
    attention = _text(data, "suggested_attention", field, 32)
    if attention not in _ATTENTION:
        raise GuidanceError(f"{field}.suggested_attention must be one of review_first, understand, routine")
    before = _text(behavior, "before", f"{field}.behavior", 1600)
    after = _text(behavior, "after", f"{field}.behavior", 1600)
    unchanged = behavior.get("unchanged", False)
    if not isinstance(unchanged, bool):
        raise GuidanceError(f"{field}.behavior.unchanged must be true or false")
    if unchanged and (before or after):
        raise GuidanceError(
            f"{field}.behavior.unchanged states no external behavior changes; it cannot carry a before/after"
        )
    return GuidanceItem(
        key=_key(data, field),
        title=_text(data, "title", field, 160, required=True),
        targets=_targets(data.get("targets"), f"{field}.targets"),
        before=before,
        after=after,
        rationale=_text(data, "rationale", field, 1600),
        concerns=_concerns(data.get("concerns"), f"{field}.concerns"),
        assumptions=_assumptions(data.get("assumptions"), f"{field}.assumptions"),
        suggested_attention=attention,
        evidence_refs=_texts(data.get("evidence_refs"), f"{field}.evidence_refs", _MAX_LIST, 240),
        internal_only=unchanged,
    )


def _decision(raw: object, field: str) -> GuidanceDecision:
    data = _mapping(raw, field)
    raw_options = _list(data.get("options"), f"{field}.options", _MAX_OPTIONS)
    if len(raw_options) < _MIN_OPTIONS:
        raise GuidanceError(f"{field}.options needs at least {_MIN_OPTIONS} meaningful alternatives")
    options: list[GuidanceOption] = []
    for index, option in enumerate(raw_options):
        where = f"{field}.options[{index}]"
        option_data = _mapping(option, where)
        options.append(
            GuidanceOption(key=_key(option_data, where), label=_text(option_data, "label", where, 160, required=True))
        )
    _unique([option.key for option in options], f"{field}.options")
    selected = data.get("selected_option")
    if selected not in {option.key for option in options}:
        raise GuidanceError(f"{field}.selected_option must be one of the option keys")
    checks = _texts(data.get("relied_checks"), f"{field}.relied_checks", _MAX_LIST, 160)
    for check in checks:
        if not _CHECK_KEY.match(check):
            raise GuidanceError(
                f"{field}.relied_checks entry {check!r} must be a logical check key browser:<scenario-id>"
            )
    return GuidanceDecision(
        key=_key(data, field),
        question=_text(data, "question", field, 600, required=True),
        options=tuple(options),
        selected_option=str(selected),
        reason_human_required=_text(data, "reason_human_required", field, 600, required=True),
        targets=_targets(data.get("targets"), f"{field}.targets"),
        assumptions=_assumptions(data.get("assumptions"), f"{field}.assumptions"),
        contract_scope=_texts(data.get("contract_scope"), f"{field}.contract_scope", _MAX_LIST, 240),
        relied_checks=checks,
    )


def parse_guidance(raw: Mapping[str, Any]) -> GuidancePayload:
    """Validate one guidance document; reject rather than truncate or guess."""

    data = _mapping(raw, "guidance")
    if data.get("schema_version") != GUIDANCE_SCHEMA_VERSION:
        raise GuidanceError(f"guidance.schema_version must be {GUIDANCE_SCHEMA_VERSION}")
    items = tuple(
        _item(item, f"items[{index}]") for index, item in enumerate(_list(data.get("items"), "items", _MAX_ITEMS))
    )
    _unique([item.key for item in items], "items")
    decisions = tuple(
        _decision(item, f"human_decisions[{index}]")
        for index, item in enumerate(_list(data.get("human_decisions"), "human_decisions", _MAX_DECISIONS))
    )
    _unique([decision.key for decision in decisions], "human_decisions")
    item_keys = {item.key for item in items}
    order = _texts(data.get("suggested_order"), "suggested_order", _MAX_ITEMS, 64)
    _unique(order, "suggested_order")
    routine = _texts(data.get("routine_candidates"), "routine_candidates", _MAX_ITEMS, 64)
    for name, keys in (("suggested_order", order), ("routine_candidates", routine)):
        unknown = [key for key in keys if key not in item_keys]
        if unknown:
            raise GuidanceError(f"{name} names unknown item keys: {', '.join(unknown)}")
    supersedes = _texts(data.get("supersedes"), "supersedes", _MAX_LIST, 64)
    return GuidancePayload(
        schema_version=GUIDANCE_SCHEMA_VERSION,
        intent=_text(data, "intent", "guidance", 600),
        items=items,
        human_decisions=decisions,
        suggested_order=order,
        routine_candidates=routine,
        supersedes=supersedes,
    )


# --------------------------------------------------------------------------- #
# canonical form
# --------------------------------------------------------------------------- #


def _target_dict(ref: GuidanceTargetRef) -> dict[str, Any]:
    return {
        "path": ref.path,
        "symbol": ref.symbol,
        "start_line": ref.start_line,
        "end_line": ref.end_line,
        "content_hash": ref.content_hash,
    }


def _assumption_dict(item: GuidanceAssumption) -> dict[str, Any]:
    return {"key": item.key, "value": item.value, "statement": item.statement, "scope": item.scope}


def _item_dict(item: GuidanceItem) -> dict[str, Any]:
    behavior: dict[str, Any] = {"before": item.before, "after": item.after}
    if item.internal_only:
        # Only when stated, so every earlier record keeps its canonical form.
        behavior["unchanged"] = True
    return {
        "key": item.key,
        "title": item.title,
        "targets": [_target_dict(ref) for ref in item.targets],
        "behavior": behavior,
        "rationale": item.rationale,
        "concerns": [{"scope": concern.scope, "reason": concern.reason} for concern in item.concerns],
        "assumptions": [_assumption_dict(assumption) for assumption in item.assumptions],
        "suggested_attention": item.suggested_attention,
        "evidence_refs": list(item.evidence_refs),
    }


def _decision_dict(decision: GuidanceDecision) -> dict[str, Any]:
    return {
        "key": decision.key,
        "question": decision.question,
        "options": [{"key": option.key, "label": option.label} for option in decision.options],
        "selected_option": decision.selected_option,
        "reason_human_required": decision.reason_human_required,
        "targets": [_target_dict(ref) for ref in decision.targets],
        "assumptions": [_assumption_dict(assumption) for assumption in decision.assumptions],
        "contract_scope": list(decision.contract_scope),
        "relied_checks": list(decision.relied_checks),
    }


def payload_to_dict(payload: GuidancePayload) -> dict[str, Any]:
    """Canonical JSON-ready form; ``parse_guidance`` of it returns *payload*."""

    return {
        "schema_version": payload.schema_version,
        "intent": payload.intent,
        "items": [_item_dict(item) for item in payload.items],
        "human_decisions": [_decision_dict(decision) for decision in payload.human_decisions],
        "suggested_order": list(payload.suggested_order),
        "routine_candidates": list(payload.routine_candidates),
        "supersedes": list(payload.supersedes),
    }


def referenced_paths(payload: GuidancePayload) -> tuple[str, ...]:
    paths = {ref.path for item in payload.items for ref in item.targets}
    paths.update(ref.path for decision in payload.human_decisions for ref in decision.targets)
    return tuple(sorted(paths))


def _all_refs(payload: GuidancePayload) -> tuple[GuidanceTargetRef, ...]:
    refs = [ref for item in payload.items for ref in item.targets]
    refs.extend(ref for decision in payload.human_decisions for ref in decision.targets)
    return tuple(refs)


# --------------------------------------------------------------------------- #
# capture
# --------------------------------------------------------------------------- #

_FILENAME = "review_guidance.jsonl"
_UNATTRIBUTED_HOST = "unattributed"


@dataclass(frozen=True)
class AuthorGuidanceRecord:
    """One immutable, attributed guidance document bound to captured content."""

    id: str
    producer_host: str
    producer_session_id: str
    producer_model: str
    attribution: str
    """``exact`` when a host session was resolved, else ``unattributed``."""
    repo_root: str
    source_identity: tuple[tuple[str, str], ...]
    """``(path, units.file_fingerprint)`` per referenced path; ``""`` when absent."""
    payload: GuidancePayload
    created_at: str


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _record_id(
    host: str,
    session_id: str,
    repo_root: str,
    identity: Sequence[tuple[str, str]],
    payload: GuidancePayload,
    predecessor: str = "",
) -> str:
    parts: list[object] = [host, session_id, repo_root, [list(pair) for pair in identity], payload_to_dict(payload)]
    if predecessor:
        # Returning to an earlier position is a new, newer record, not a no-op.
        parts.append(predecessor)
    return "gdn-" + hashlib.sha256(_canonical(parts).encode("utf-8")).hexdigest()[:24]


def _record_dict(record: AuthorGuidanceRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "producer_host": record.producer_host,
        "producer_session_id": record.producer_session_id,
        "producer_model": record.producer_model,
        "attribution": record.attribution,
        "repo_root": record.repo_root,
        "source_identity": [list(pair) for pair in record.source_identity],
        "payload": payload_to_dict(record.payload),
        "created_at": record.created_at,
    }


def _record_from_dict(raw: Mapping[str, Any]) -> AuthorGuidanceRecord | None:
    try:
        identity_raw = raw.get("source_identity") or []
        identity = tuple(
            (str(pair[0]), str(pair[1])) for pair in identity_raw if isinstance(pair, list | tuple) and len(pair) == 2
        )
        payload_raw = raw.get("payload")
        if not isinstance(payload_raw, Mapping):
            return None
        record = AuthorGuidanceRecord(
            id=str(raw.get("id") or ""),
            producer_host=str(raw.get("producer_host") or ""),
            producer_session_id=str(raw.get("producer_session_id") or ""),
            producer_model=str(raw.get("producer_model") or ""),
            attribution=str(raw.get("attribution") or "unattributed"),
            repo_root=str(raw.get("repo_root") or ""),
            source_identity=identity,
            payload=parse_guidance(payload_raw),
            created_at=str(raw.get("created_at") or ""),
        )
    except (GuidanceError, TypeError, ValueError, IndexError):
        return None
    if not record.id or not record.repo_root:
        return None
    return record


def _sidecar_dir(store_root: Path, repo_root: Path, host: str, session_id: str) -> Path:
    if host and session_id:
        return session_dir(store_root, host, session_id)
    token = "repo-" + hashlib.sha256(str(repo_root).encode("utf-8")).hexdigest()[:16]
    return session_dir(store_root, _UNATTRIBUTED_HOST, token)


def record_author_guidance(
    store_root: Path,
    repo_root: Path,
    *,
    host: str,
    session_id: str,
    model: str = "",
    guidance: Mapping[str, Any],
) -> AuthorGuidanceRecord:
    """Validate and append one guidance document to its producer's sidecar.

    Each referenced file is fingerprinted *now* with the same recipe as the
    Snapshot file unit, so read time can tell current references from stale
    ones exactly. Nothing here opens, creates or advances a Review.
    """

    payload = parse_guidance(guidance)
    root = repo_root.resolve()
    identity: list[tuple[str, str]] = []
    texts: dict[str, str] = {}
    for path in referenced_paths(payload):
        try:
            target = confine_to_root(root / path, root)
        except ValueError as exc:
            raise GuidanceError(f"target path {path!r} escapes the repository") from exc
        if target.is_file():
            text = target.read_text(encoding="utf-8", errors="replace")
            texts[path] = text
            identity.append((path, file_fingerprint(path, text)))
        else:
            # A deleted or not-yet-created file is legitimate guidance context;
            # read time reports it as such instead of refusing the handoff.
            identity.append((path, ""))
    for ref in _all_refs(payload):
        current = texts.get(ref.path)
        if current is None or not ref.start_line:
            continue
        total = len(current.splitlines())
        if max(ref.start_line, ref.end_line) > total:
            raise GuidanceError(
                f"{ref.path} has {total} line(s); target lines {ref.start_line}-{ref.end_line} run past the end"
            )

    exact = bool(host and session_id)
    producer_host = host if exact else ""
    producer_session = session_id if exact else ""
    record = AuthorGuidanceRecord(
        id=_record_id(producer_host, producer_session, str(root), identity, payload),
        producer_host=producer_host,
        producer_session_id=producer_session,
        producer_model=model,
        attribution="exact" if exact else "unattributed",
        repo_root=str(root),
        source_identity=tuple(identity),
        payload=payload,
        created_at=datetime.now(UTC).isoformat(),
    )
    sidecar = _sidecar_dir(store_root, root, producer_host, producer_session) / _FILENAME
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    same_repo = [existing for existing in _read_sidecar(sidecar) if existing.repo_root == record.repo_root]
    if same_repo:
        newest = same_repo[-1]
        if newest.source_identity == record.source_identity and payload_to_dict(newest.payload) == payload_to_dict(
            payload
        ):
            return newest  # the producer's current position, recorded again
        if any(existing.id == record.id for existing in same_repo):
            record = replace(
                record,
                id=_record_id(producer_host, producer_session, str(root), identity, payload, predecessor=newest.id),
            )
    with sidecar.open("a", encoding="utf-8") as fh:
        fh.write(_canonical(_record_dict(record)) + "\n")
    return record


def _read_sidecar(path: Path) -> tuple[AuthorGuidanceRecord, ...]:
    if not path.is_file():
        return ()
    try:
        # Split on "\n" only: JSON leaves U+2028/U+2029/U+0085 unescaped and
        # ``str.splitlines`` would cut a record in half at them.
        lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
    except OSError:
        return ()
    out: list[AuthorGuidanceRecord] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(raw, Mapping) and (record := _record_from_dict(raw)) is not None:
            out.append(record)
    return tuple(out)


# --------------------------------------------------------------------------- #
# import at Snapshot capture
# --------------------------------------------------------------------------- #

_MAX_SIDECARS = 256


def record_from_row(row: AuthorGuidanceRow) -> AuthorGuidanceRecord:
    """The stored document as the record it was captured as."""

    identity = json.loads(row.source_identity_json or "[]")
    return AuthorGuidanceRecord(
        id=row.id,
        producer_host=row.producer_host,
        producer_session_id=row.producer_session_id,
        producer_model=row.producer_model,
        attribution=row.attribution,
        repo_root=row.repo_root,
        source_identity=tuple((str(pair[0]), str(pair[1])) for pair in identity),
        payload=parse_guidance(json.loads(row.payload_json)),
        created_at=row.created_at,
    )


def load_guidance_records(rows: Sequence[AuthorGuidanceRow]) -> tuple[tuple[AuthorGuidanceRecord, ...], int]:
    """Stored rows as records, plus how many could not be read.

    A row from a newer schema or a corrupt row is skipped and counted, never
    allowed to take down every other author's guidance with it.
    """

    records: list[AuthorGuidanceRecord] = []
    unreadable = 0
    for row in rows:
        try:
            records.append(record_from_row(row))
        except (GuidanceError, TypeError, ValueError, IndexError, KeyError):
            unreadable += 1
    return tuple(records), unreadable


def _other_sidecars(store_root: Path, exact: Path | None) -> list[Path]:
    try:
        paths = [path for path in (Path(store_root) / "sessions").glob("*/*/*/*/*/" + _FILENAME) if path != exact]
        paths.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    except OSError:
        return []
    # lc-debt: newest 256 sidecars only; an index of guidance by repo_root would lift the bound.
    return paths[:_MAX_SIDECARS]


def import_author_guidance(
    store: ReviewStore,
    session: ReviewSession,
    revision: ReviewRevision,
    *,
    store_root: Path,
    repo_root: Path,
    changed_paths: Collection[str] | None = None,
) -> int:
    """Store guidance that belongs to this Change; returns the rows added.

    The revision's exact provenance session's guidance belongs when it is
    about this Change: it names at least one changed path (new or old side) or
    names no path at all -- one session often spans several Changes. Any other
    producer's guidance belongs only when it was captured against content this
    Snapshot actually contains (a referenced file's fingerprint equals this
    revision's file unit). Import never touches a human state.
    """

    root = str(repo_root.resolve())
    exact_path: Path | None = None
    if revision.provenance_certainty == "exact" and revision.provenance_host and revision.provenance_session_id:
        exact_path = session_dir(store_root, revision.provenance_host, revision.provenance_session_id) / _FILENAME
    file_units = [unit for unit in store.list_units(revision.id) if unit.kind == "file"]
    file_prints = {unit.content_fingerprint for unit in file_units}
    changed = set(changed_paths) if changed_paths is not None else {unit.path for unit in file_units}
    bound: list[AuthorGuidanceRecord] = []
    if exact_path is not None:
        for record in _read_sidecar(exact_path):
            paths = referenced_paths(record.payload)
            if record.repo_root == root and (not paths or any(path in changed for path in paths)):
                bound.append(record)
    for path in _other_sidecars(store_root, exact_path):
        bound.extend(
            record
            for record in _read_sidecar(path)
            if record.repo_root == root
            and any(fingerprint and fingerprint in file_prints for _, fingerprint in record.source_identity)
        )

    added = 0
    imported_at = datetime.now(UTC).isoformat()
    for record in bound:
        row = AuthorGuidanceRow(
            review_id=session.id,
            id=record.id,
            revision_id=revision.id,
            producer_host=record.producer_host,
            producer_session_id=record.producer_session_id,
            producer_model=record.producer_model,
            attribution=record.attribution,
            repo_root=record.repo_root,
            schema_version=record.payload.schema_version,
            source_identity_json=_canonical([list(pair) for pair in record.source_identity]),
            payload_json=_canonical(payload_to_dict(record.payload)),
            created_at=record.created_at,
            imported_at=imported_at,
        )
        if store.add_author_guidance(row):
            added += 1
    return added


# --------------------------------------------------------------------------- #
# read-time view: resolution, supersession, conflict
# --------------------------------------------------------------------------- #

RefOutcome = Literal[
    "resolved", "stale_content", "ambiguous", "symbol_not_found", "not_in_change", "deleted_file", "renamed"
]
GuidanceKind = Literal["item", "decision"]

_UNATTRIBUTED_PRODUCER = "unattributed"
_UNATTRIBUTED_LIMITATION = (
    "Some guidance has no host session attribution. All unattributed guidance is treated as one producer, "
    "so a later document replaces an earlier one and disagreements among them cannot be shown as conflicts."
)
_NO_PACKET_LIMITATION = "The change's file list is unavailable, so deleted and renamed files cannot be recognized."


@dataclass(frozen=True)
class RefResolution:
    """What one author reference points at in this Snapshot, stated, never guessed."""

    ref: GuidanceTargetRef
    outcome: RefOutcome
    target_ids: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class GuidanceDefinition:
    """One producer's current definition of one keyed item or decision."""

    kind: GuidanceKind
    key: str
    record_id: str
    producer: str
    attribution: str
    created_at: str
    stale: bool
    """A referenced file's content differs from what the author saw."""
    item: GuidanceItem | None
    decision: GuidanceDecision | None
    resolutions: tuple[RefResolution, ...]
    source: tuple[tuple[str, str], ...] = ()
    """Captured ``(path, fingerprint)`` of the paths this definition names."""


@dataclass(frozen=True)
class GuidanceConflict:
    """Producers disagree about one key against the same content; nothing wins."""

    kind: GuidanceKind
    key: str
    definitions: tuple[GuidanceDefinition, ...]
    reason: str = "author_guidance_conflict"
    stale: bool = False
    """Every side of the disagreement was written for earlier content."""


SupersessionReason = Literal["same_producer", "explicit", "newer_source", "same_meaning"]


@dataclass(frozen=True)
class GuidanceSupersession:
    """A definition that is no longer current, and why; kept for history."""

    record_id: str
    kind: GuidanceKind
    key: str
    by_record_id: str
    reason: SupersessionReason


@dataclass(frozen=True)
class GuidanceView:
    records: tuple[AuthorGuidanceRecord, ...]
    items: tuple[GuidanceDefinition, ...]
    decisions: tuple[GuidanceDefinition, ...]
    conflicts: tuple[GuidanceConflict, ...]
    superseded: tuple[GuidanceSupersession, ...]
    limitations: tuple[str, ...]


def _target_ids(targets: Sequence[ReviewTarget]) -> tuple[str, ...]:
    return tuple(target.target_id for target in targets)


def _overlaps(target: ReviewTarget, start: int, end: int) -> bool:
    return any(span.side == "new" and span.start_line <= end and start <= span.end_line for span in target.spans)


def resolve_ref(
    ref: GuidanceTargetRef,
    *,
    captured_fingerprint: str,
    targets: Sequence[ReviewTarget],
    file_units: Mapping[str, ReviewUnit],
    files: Mapping[str, Mapping[str, Any]],
) -> RefResolution:
    """Resolve one reference against this Snapshot's targets."""

    on_path = [target for target in targets if target.path == ref.path]
    row = files.get(ref.path)
    if row is None:
        for path, candidate in files.items():
            if candidate.get("status") == "renamed" and candidate.get("old_path") == ref.path:
                moved = [target for target in targets if target.path == path]
                return RefResolution(ref, "renamed", _target_ids(moved), f"renamed to {path}")
        return RefResolution(ref, "not_in_change", (), "this change does not touch the file")
    if row.get("status") == "deleted":
        return RefResolution(ref, "deleted_file", _target_ids(on_path), "this change deletes the file")
    unit = file_units.get(ref.path)
    if captured_fingerprint and unit is not None and unit.content_fingerprint != captured_fingerprint:
        return RefResolution(
            ref, "stale_content", _target_ids(on_path), "the file changed after the author wrote this guidance"
        )
    if ref.symbol:
        named = [
            target for target in on_path if target.symbol == ref.symbol or target.symbol.endswith("." + ref.symbol)
        ]
        if not named:
            return RefResolution(ref, "symbol_not_found", (), f"no changed symbol named {ref.symbol}")
        if len(named) > 1 and ref.start_line:
            narrowed = [target for target in named if _overlaps(target, ref.start_line, ref.end_line)]
            named = narrowed if len(narrowed) == 1 else named
        if len(named) > 1:
            return RefResolution(
                ref, "ambiguous", _target_ids(named), f"{len(named)} changed symbols match {ref.symbol}"
            )
        return RefResolution(ref, "resolved", _target_ids(named))
    if ref.start_line:
        spanned = [target for target in on_path if _overlaps(target, ref.start_line, ref.end_line)]
        return RefResolution(ref, "resolved", _target_ids(spanned), "" if spanned else "no changed lines in this range")
    return RefResolution(ref, "resolved", _target_ids(on_path))


def producer_key(record: AuthorGuidanceRecord) -> str:
    if record.attribution == "exact" and record.producer_host and record.producer_session_id:
        return f"{record.producer_host}:{record.producer_session_id}"
    return _UNATTRIBUTED_PRODUCER


def _files(packet: Mapping[str, Any] | None, file_units: Mapping[str, ReviewUnit]) -> dict[str, Mapping[str, Any]]:
    if not isinstance(packet, Mapping):
        return {path: {"path": path, "status": "modified"} for path in file_units}
    rows: dict[str, Mapping[str, Any]] = {}
    for row in packet.get("files") or ():
        if isinstance(row, Mapping) and isinstance(row.get("path"), str):
            rows[row["path"]] = row
    return rows


def _signature(definition: GuidanceDefinition) -> tuple[object, ...]:
    """What must agree for two producers' definitions to be compatible.

    Semantic values only: wording (question, labels, before/after prose,
    rationale, statements) never makes two definitions incompatible.
    """

    if definition.decision is not None:
        decision = definition.decision
        return (
            frozenset(option.key for option in decision.options),
            decision.selected_option,
            frozenset((item.key, item.value) for item in decision.assumptions),
        )
    item = definition.item
    if item is None:
        return ()
    return (
        frozenset((ref.path, ref.symbol, ref.start_line, ref.end_line) for ref in item.targets),
        frozenset((assumption.key, assumption.value) for assumption in item.assumptions),
    )


def _newest(definitions: Sequence[GuidanceDefinition]) -> GuidanceDefinition:
    return max(definitions, key=lambda item: (item.created_at, item.record_id))


def _unreadable_limitation(count: int) -> str:
    noun = "record uses" if count == 1 else "records use"
    verb = "is" if count == 1 else "are"
    return f"{count} stored guidance {noun} an unsupported or corrupt format and {verb} not shown."


def build_guidance_view(
    records: Sequence[AuthorGuidanceRecord],
    *,
    targets: Sequence[ReviewTarget],
    units: Sequence[ReviewUnit],
    packet: Mapping[str, Any] | None,
    unreadable: int = 0,
) -> GuidanceView:
    """Interpret stored guidance against one Snapshot.

    Per producer the newest definition of a key wins (or an explicit
    ``supersedes`` retires the producer's earlier record). Across producers,
    a definition written for newer source replaces older-source ones;
    definitions against the same source must agree on semantic values, or they
    become one conflict and no definition -- never last-writer-wins. Everything
    replaced stays in ``superseded`` with its reason.
    """

    ordered = sorted(records, key=lambda record: (record.created_at, record.id))
    file_units = {unit.path: unit for unit in units if unit.kind == "file"}
    files = _files(packet, file_units)
    current: dict[tuple[str, str, str], GuidanceDefinition] = {}
    superseded: list[GuidanceSupersession] = []
    order: list[tuple[GuidanceKind, str]] = []

    for record in ordered:
        producer = producer_key(record)
        captured = dict(record.source_identity)
        retired = set(record.payload.supersedes)
        for slot, definition in list(current.items()):
            if slot[0] == producer and definition.record_id in retired:
                superseded.append(
                    GuidanceSupersession(definition.record_id, definition.kind, definition.key, record.id, "explicit")
                )
                del current[slot]
        entries: list[tuple[GuidanceKind, str, GuidanceItem | None, GuidanceDecision | None]] = [
            ("item", item.key, item, None) for item in record.payload.items
        ]
        entries.extend(("decision", decision.key, None, decision) for decision in record.payload.human_decisions)
        for kind, key, item, decision in entries:
            refs = item.targets if item is not None else decision.targets if decision is not None else ()
            stale = any(
                captured.get(ref.path)
                and ref.path in file_units
                and file_units[ref.path].content_fingerprint != captured[ref.path]
                for ref in refs
            )
            definition = GuidanceDefinition(
                kind=kind,
                key=key,
                record_id=record.id,
                producer=producer,
                attribution=record.attribution,
                created_at=record.created_at,
                stale=stale,
                item=item,
                decision=decision,
                resolutions=tuple(
                    resolve_ref(
                        ref,
                        captured_fingerprint=captured.get(ref.path, ""),
                        targets=targets,
                        file_units=file_units,
                        files=files,
                    )
                    for ref in refs
                ),
                source=tuple(sorted({(ref.path, captured.get(ref.path, "")) for ref in refs})),
            )
            slot = (producer, kind, key)
            previous = current.get(slot)
            if previous is not None:
                superseded.append(GuidanceSupersession(previous.record_id, kind, key, record.id, "same_producer"))
            current[slot] = definition
            if (kind, key) not in order:
                order.append((kind, key))

    items: list[GuidanceDefinition] = []
    decisions: list[GuidanceDefinition] = []
    conflicts: list[GuidanceConflict] = []
    for kind, key in order:
        definitions = [definition for (_, k, name), definition in current.items() if (k, name) == (kind, key)]
        if not definitions:
            continue
        fresh = [definition for definition in definitions if not definition.stale]
        if fresh:
            pool = fresh
        else:
            # Everything was written for earlier content: the newest source
            # identity is current, and within it producers must still agree.
            newest_source = _newest(definitions).source
            pool = [definition for definition in definitions if definition.source == newest_source]
        older = [definition for definition in definitions if definition not in pool]
        if len({_signature(definition) for definition in pool}) > 1:
            ranked = tuple(sorted(pool, key=lambda item: (item.created_at, item.record_id)))
            conflicts.append(GuidanceConflict(kind=kind, key=key, definitions=ranked, stale=not fresh))
            replaced_by = ranked[-1].record_id
        else:
            winner = _newest(pool)
            (items if kind == "item" else decisions).append(winner)
            replaced_by = winner.record_id
            superseded.extend(
                GuidanceSupersession(definition.record_id, kind, key, winner.record_id, "same_meaning")
                for definition in pool
                if definition is not winner
            )
        superseded.extend(
            GuidanceSupersession(definition.record_id, kind, key, replaced_by, "newer_source") for definition in older
        )

    limitations: list[str] = []
    if any(producer_key(record) == _UNATTRIBUTED_PRODUCER for record in ordered):
        limitations.append(_UNATTRIBUTED_LIMITATION)
    if ordered and not isinstance(packet, Mapping):
        limitations.append(_NO_PACKET_LIMITATION)
    if unreadable:
        limitations.append(_unreadable_limitation(unreadable))
    return GuidanceView(
        records=tuple(ordered),
        items=tuple(items),
        decisions=tuple(decisions),
        conflicts=tuple(conflicts),
        superseded=tuple(superseded),
        limitations=tuple(limitations),
    )


def _definition_dict(definition: GuidanceDefinition) -> dict[str, Any]:
    return {
        "kind": definition.kind,
        "key": definition.key,
        "record_id": definition.record_id,
        "producer": definition.producer,
        "attribution": definition.attribution,
        "created_at": definition.created_at,
        "stale": definition.stale,
        "item": _item_dict(definition.item) if definition.item is not None else None,
        "decision": _decision_dict(definition.decision) if definition.decision is not None else None,
        "resolutions": [
            {
                "ref": _target_dict(resolution.ref),
                "outcome": resolution.outcome,
                "target_ids": list(resolution.target_ids),
                "detail": resolution.detail,
            }
            for resolution in definition.resolutions
        ],
    }


def guidance_view_payload(view: GuidanceView) -> dict[str, Any]:
    """JSON-ready view. The local ``repo_root`` is deliberately not exposed."""

    return {
        "schema_version": GUIDANCE_SCHEMA_VERSION,
        "records": [
            {
                "id": record.id,
                "producer_host": record.producer_host,
                "producer_session_id": record.producer_session_id,
                "producer_model": record.producer_model,
                "attribution": record.attribution,
                "created_at": record.created_at,
                "intent": record.payload.intent,
                "suggested_order": list(record.payload.suggested_order),
                "routine_candidates": list(record.payload.routine_candidates),
                "supersedes": list(record.payload.supersedes),
            }
            for record in view.records
        ],
        "items": [_definition_dict(definition) for definition in view.items],
        "decisions": [_definition_dict(definition) for definition in view.decisions],
        "conflicts": [
            {
                "kind": conflict.kind,
                "key": conflict.key,
                "reason": conflict.reason,
                "stale": conflict.stale,
                "definitions": [_definition_dict(definition) for definition in conflict.definitions],
            }
            for conflict in view.conflicts
        ],
        "superseded": [
            {
                "record_id": item.record_id,
                "kind": item.kind,
                "key": item.key,
                "superseded_by": item.by_record_id,
                "reason": item.reason,
            }
            for item in view.superseded
        ],
        "limitations": list(view.limitations),
    }
