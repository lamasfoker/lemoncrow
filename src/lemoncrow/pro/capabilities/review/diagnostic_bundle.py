"""One redacted diagnostic bundle for Review support (G8-DIAG).

One builder serves both surfaces: ``lc review diagnostics`` (local) and
``GET /v1/admin/reviews/diagnostic-bundle`` (hosted, organization admins).
Collectors read their own stores into the typed records below -- the local
one in :mod:`lemoncrow.pro.capabilities.review.diagnostic_local`, the hosted
one in ``lemoncrow_server.review_diagnostics`` -- and :func:`build_diagnostic_bundle`
turns them into the bundle. The surfaces differ only in which collectors are
available: every section in :data:`SECTIONS` is always present, either
``populated``, ``unavailable`` (it could not be collected) or ``unknown`` (no
component reports that state yet), the last two with a reason code from
:data:`UNAVAILABLE_REASONS`.

Redaction is by construction, not by scrubbing a finished document:

* A record field reaches the bundle only when :data:`RECORD_FIELDS` names it,
  with a kind (``id``, ``enum``, ``count``, ``time``) whose sanitizer refuses
  any value outside that shape. Records carry their free text (``error``,
  ``degraded``) and ``model`` separately; those are never emitted as fields.
* Unavoidable free text -- error messages and degradation reasons -- goes
  through one pass, :func:`redact_text`. What survives: words of
  :data:`LEXICON` (lower-cased), numbers of at most three digits and the
  punctuation in :data:`_KEPT_PUNCTUATION`. Every other run becomes ``…`` and
  the result is at most :data:`MAX_TEXT_CHARS` characters. A digest keyed per
  bundle groups identical messages without making them guessable.
* An extra item appears only when its name is passed in ``include`` and the
  surface allows it (:data:`INCLUDE_OPTIONS`); the bundle records what was
  requested, by whom and when. Unknown names are refused.

Bounds (:class:`DiagnosticLimits`) cap every list: the N most recently updated
Reviews across at most ``repositories`` repository stores, and the jobs,
provider operations and degradations of those Reviews only.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import platform
import re
import secrets
import unicodedata
from bisect import bisect_left
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from lemoncrow.pro.capabilities.review.data_flow import CATEGORIES, DATA_FLOW_SCHEMA_VERSION, FLOW_STATUSES
from lemoncrow.pro.capabilities.review.data_governance import shown_model_name
from lemoncrow.pro.capabilities.review.diagnostic_secrets import NO_SECRETS, SecretGuard, decode_escapes, match_key
from lemoncrow.pro.capabilities.review.diagnostic_state import GateState, ReleaseState
from lemoncrow.pro.capabilities.review.model_identity import clean_model_name

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.data_flow import DataFlowProjection

BUNDLE_SCHEMA: Final[str] = "lemoncrow.review.diagnostic_bundle"
BUNDLE_SCHEMA_VERSION: Final[int] = 2
LEXICON_VERSION: Final[int] = 1

Surface = Literal["local", "hosted"]

#: Every section, in bundle order. None is ever omitted.
SECTIONS: Final[tuple[str, ...]] = (
    "versions",
    "protocol",
    "data_flow",
    "execution_locus",
    "gates",
    "snapshots",
    "intelligence_jobs",
    "queue_jobs",
    "job_lifecycle",
    "background_budget",
    "provider_operations",
    "webhook_deliveries",
    "degradations",
    "recent_errors",
)

#: Why a section (or part of one) is unavailable. Collectors pass a code, never text.
UNAVAILABLE_REASONS: Final[Mapping[str, str]] = {
    "no_review_store": "no Review store was found for this surface",
    "store_unreadable": "the Review store could not be read",
    "no_durable_queue": "this surface runs Review jobs without a durable job queue",
    "queue_unreadable": "the durable job queue could not be read",
    "no_provider_webhooks": "local Review receives no SCM provider webhooks",
    "not_a_repository": "data flow is per repository and no Git repository was given",
    "no_repositories": "no repository holds Review state yet",
    "data_flow_failed": "the data-flow projection could not be computed",
    "server_not_configured": "no LemonCrow server is configured",
    "server_unreachable": "the configured LemonCrow server did not answer discovery",
    "no_server_manifest": "the server publishes no Review capability manifest",
    "not_applicable_hosted": "a hosted server has no client protocol window of its own",
    "derived_unavailable": "every section this one is derived from is unavailable",
    "no_managed_worker": "no managed Review worker is configured for this server",
    "worker_unreachable": "the managed Review worker did not answer its compatibility probe",
    "job_lifecycle_not_reported": "job lifecycle state not yet reported",
    "background_budget_not_reported": "background budget state not yet reported",
    "job_queue_not_postgres": "this job queue is not the PostgreSQL authority, which alone reports lifecycle state",
    "job_queue_unreadable": "the PostgreSQL job queue could not be read for lifecycle state",
    "gates_not_reported": "no gate state was collected for this bundle",
    "gate_unreadable": "this gate's configuration could not be read",
    "release_not_reported": "release qualification state not yet reported",
}

INCLUDE_ERROR_TEXT: Final[str] = "error-text"
INCLUDE_MODEL_NAMES: Final[str] = "model-names"
#: The only extra items a bundle can carry, and exactly what each adds.
INCLUDE_OPTIONS: Final[Mapping[str, str]] = {
    INCLUDE_ERROR_TEXT: (
        "recent_errors[].text: each error message, at most 1000 characters, with environment values, "
        "URL credentials and query strings, bearer and API-key shapes, 16+ character letter-and-digit runs, "
        "e-mail addresses and local paths removed"
    ),
    INCLUDE_MODEL_NAMES: (
        "intelligence_jobs[].model: the model id each AI job ran with (an id that is not a plain [vendor/]name[:tag] "
        "model identifier, or that equals, contains or sits inside a configured secret value, is withheld)"
    ),
}
#: ``lc review diagnostics`` accepts every option. The hosted endpoint names the same
#: two, but each is a privileged explicit include: none is on by default, and the route
#: grants one only to a caller holding the diagnostics permission. Hosted error text can
#: carry operator-side detail, so it passes the same redaction as local and is bounded.
LOCAL_INCLUDES: Final[tuple[str, ...]] = tuple(INCLUDE_OPTIONS)
HOSTED_INCLUDES: Final[tuple[str, ...]] = tuple(INCLUDE_OPTIONS)

MAX_TEXT_CHARS: Final[int] = 160
MAX_REASON_CHARS: Final[int] = 120
#: Characters of raw text a redaction pass reads at all (cost bound).
MAX_TEXT_INPUT_CHARS: Final[int] = 4000
MAX_INCLUDED_TEXT_CHARS: Final[int] = 1000
WITHHELD: Final[str] = "<withheld>"
PLACEHOLDER: Final[str] = "…"


@dataclass(frozen=True, slots=True)
class DiagnosticLimits:
    """Every bound of one bundle. Collectors fetch at most ``n + 1`` to detect truncation."""

    snapshots: int = 20
    jobs: int = 50
    operations: int = 50
    webhooks: int = 20
    degradations: int = 20
    reasons_per_snapshot: int = 16
    errors: int = 25
    repositories: int = 25
    data_flow_repositories: int = 3

    def to_wire(self) -> dict[str, int]:
        return {
            "snapshots": self.snapshots,
            "jobs": self.jobs,
            "operations": self.operations,
            "webhooks": self.webhooks,
            "degradations": self.degradations,
            "reasons_per_snapshot": self.reasons_per_snapshot,
            "errors": self.errors,
            "repositories": self.repositories,
            "data_flow_repositories": self.data_flow_repositories,
        }


DEFAULT_LIMITS: Final[DiagnosticLimits] = DiagnosticLimits()


# --------------------------------------------------------------------------- #
# records (what collectors produce)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SessionRow:
    """The identity of one Review a store reader lists (recency order)."""

    review_id: str
    status: str
    current_revision_id: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class SnapshotRecord:
    review_id: str
    repo_id: str
    status: str
    snapshot_id: str = ""
    snapshot_number: int | None = None
    capture_kind: str = ""
    packet_schema_version: int | None = None
    snapshot_created_at: str = ""
    review_updated_at: str = ""
    #: Raw degradation reasons; emitted only through :func:`redact_text`.
    degraded: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class IntelligenceJobRecord:
    job_id: str
    kind: str
    review_id: str
    repo_id: str
    snapshot_id: str = ""
    capability: str = ""
    driver: str = ""
    state: str = ""
    model_calls: int | None = None
    created_at: str = ""
    completed_at: str = ""
    #: Emitted only with ``include=model-names``.
    model: str = ""
    #: Raw error text; emitted only through :func:`redact_text` (or the error-text include).
    error: str = ""


@dataclass(frozen=True, slots=True)
class QueueJobRecord:
    job_id: str
    repo_id: str
    review_id: str = ""
    snapshot_id: str = ""
    capability: str = ""
    execution_class: str = ""
    execution_locus: str = ""
    state: str = ""
    attempt: int | None = None
    retry_count: int | None = None
    reclaim_count: int | None = None
    publication_state: str = ""
    created_at: str | float | None = None
    completed_at: str | float | None = None
    error: str = ""
    publication_error: str = ""


@dataclass(frozen=True, slots=True)
class ProviderOperationRecord:
    operation_id: str
    review_id: str
    repo_id: str
    snapshot_id: str = ""
    provider: str = ""
    kind: str = ""
    state: str = ""
    attempts: int | None = None
    #: The provider's own id for the operation, when it is opaque-id shaped.
    remote_ref: str = ""
    created_at: str = ""
    updated_at: str = ""
    error: str = ""


@dataclass(frozen=True, slots=True)
class WebhookDeliveryRecord:
    delivery_id: str
    repo_id: str
    review_id: str = ""
    provider: str = ""
    event_kind: str = ""
    state: str = ""
    attempts: int | None = None
    created_at: str = ""
    failed_at: str = ""
    error: str = ""


@dataclass(frozen=True, slots=True)
class RecordSource:
    """One record section as collected: records, or why there are none."""

    records: tuple[Any, ...] = ()
    unavailable: str = ""
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class VersionFacts:
    #: ``(distribution, version)``; ``not_installed`` when absent.
    components: tuple[tuple[str, str], ...] = ()
    #: Store schema markers seen, e.g. ``session:1``, ``migration:v3_037_...``, ``postgres:31``.
    store_schemas: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProtocolFacts:
    #: ``(N, N-1)`` this process speaks as a client; ``None`` with ``client_reason`` otherwise.
    client_window: tuple[str, str] | None = None
    client_reason: str = ""
    #: The server's discovery/identity document; only allowlisted keys are read.
    server_identity: Mapping[str, Any] | None = None
    server_reason: str = ""
    #: Hosted managed-worker compatibility projection; collector supplies content-free fields only.
    managed_worker: Mapping[str, Any] | None = None
    #: Why there is no ``managed_worker`` (``no_managed_worker``, ``worker_unreachable``); ``""`` when none is expected.
    worker_reason: str = ""


@dataclass(frozen=True, slots=True)
class DataFlowFacts:
    #: ``(repository label, projection)`` per repository described.
    projections: tuple[tuple[str, DataFlowProjection], ...] = ()
    unavailable: str = ""
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class ReviewRecords:
    snapshots: RecordSource
    intelligence_jobs: RecordSource
    provider_operations: RecordSource
    webhook_deliveries: RecordSource
    repositories_scanned: int = 0
    repositories_unreadable: int = 0


@dataclass(frozen=True, slots=True)
class ReportedState:
    """State another component reports (job lifecycle, background budget); ``unknown`` is a registered reason."""

    counts: tuple[tuple[str, int], ...] = ()
    words: tuple[tuple[str, str], ...] = ()
    unknown: str = ""


@dataclass(frozen=True, slots=True)
class OperationsFacts:
    """Gate, release, job-lifecycle and budget state. Every default says "not reported", never "fine"."""

    gates: tuple[GateState, ...] = ()
    release: ReleaseState = field(default_factory=ReleaseState)
    job_lifecycle: ReportedState = field(default_factory=lambda: ReportedState(unknown="job_lifecycle_not_reported"))
    background_budget: ReportedState = field(
        default_factory=lambda: ReportedState(unknown="background_budget_not_reported")
    )


@dataclass(frozen=True, slots=True)
class DiagnosticFacts:
    surface: Surface
    versions: VersionFacts
    protocol: ProtocolFacts
    data_flow: DataFlowFacts
    records: ReviewRecords
    queue_jobs: RecordSource
    organization: str = ""
    repository: str = ""
    repositories_total: int = 0
    operations: OperationsFacts = field(default_factory=OperationsFacts)


class ReviewStoreReader(Protocol):
    """Bounded reads of one repository's Review store (local SQLite or hosted)."""

    def recent_sessions(self, limit: int) -> Sequence[SessionRow]: ...

    def snapshot(self, session: SessionRow) -> SnapshotRecord: ...

    def intelligence_jobs(self, review_id: str, limit: int) -> Sequence[IntelligenceJobRecord]: ...

    def provider_operations(self, review_id: str, limit: int) -> Sequence[ProviderOperationRecord]: ...

    def webhook_deliveries(self, limit: int) -> Sequence[WebhookDeliveryRecord]: ...


def order_by_recency(activity: Iterable[tuple[str, object]]) -> list[str]:
    """Repository ids, the one whose Review was durably updated most recently first.

    *activity* is ``(repository id, newest Review updated_at)``: the durable
    update time the Review store records, never a file time and never the id.
    A repository with no readable update time comes last; ties break by id.
    Both surfaces order their repositories here, so no list is cut by id order.
    """

    ordered = sorted(((repo_id, _time(updated_at)) for repo_id, updated_at in activity), key=lambda item: item[0])
    return [repo_id for repo_id, _when in sorted(ordered, key=lambda item: item[1], reverse=True)]


def collect_review_records(
    readers: Sequence[tuple[str, ReviewStoreReader]],
    *,
    limits: DiagnosticLimits = DEFAULT_LIMITS,
    webhooks: bool,
    unavailable: str = "",
) -> ReviewRecords:
    """The N most recent Reviews across *readers*, and the jobs and operations of those Reviews only.

    Each reader is asked for at most ``limit + 1`` rows, so the cost is bounded
    by the limits, never by store size. A reader that raises counts as
    unreadable; when every reader is unreadable the sections say so.
    """

    webhooks_off = RecordSource(unavailable="no_provider_webhooks")
    if unavailable:
        gone = RecordSource(unavailable=unavailable)
        return ReviewRecords(gone, gone, gone, gone if webhooks else webhooks_off)
    candidates: list[tuple[SessionRow, ReviewStoreReader]] = []
    readable: list[ReviewStoreReader] = []
    unreadable = 0
    for _repo_id, reader in readers:
        try:
            rows = tuple(reader.recent_sessions(limits.snapshots + 1))
        except Exception:
            unreadable += 1
            continue
        readable.append(reader)
        candidates.extend((row, reader) for row in rows)
    if readers and not readable:
        gone = RecordSource(unavailable="store_unreadable")
        return ReviewRecords(
            gone, gone, gone, gone if webhooks else webhooks_off, len(readers), repositories_unreadable=unreadable
        )
    candidates.sort(key=lambda item: (_time(item[0].updated_at), item[0].review_id), reverse=True)
    snapshots: list[SnapshotRecord] = []
    jobs: list[IntelligenceJobRecord] = []
    operations: list[ProviderOperationRecord] = []
    for row, reader in candidates[: limits.snapshots]:
        try:
            snapshots.append(reader.snapshot(row))
            jobs.extend(reader.intelligence_jobs(row.review_id, limits.jobs + 1))
            operations.extend(reader.provider_operations(row.review_id, limits.operations + 1))
        except Exception:
            # lc-debt: a Review whose rows cannot be read is skipped, counted only through
            # repositories_unreadable when its whole store fails; upgrade path: a per-Review note.
            continue
    deliveries: list[WebhookDeliveryRecord] = []
    if webhooks:
        for reader in readable:
            try:
                deliveries.extend(reader.webhook_deliveries(limits.webhooks + 1))
            except Exception:
                continue
    return ReviewRecords(
        snapshots=RecordSource(tuple(snapshots), truncated=len(candidates) > limits.snapshots),
        intelligence_jobs=RecordSource(tuple(jobs)),
        provider_operations=RecordSource(tuple(operations)),
        webhook_deliveries=RecordSource(tuple(deliveries)) if webhooks else webhooks_off,
        repositories_scanned=len(readers),
        repositories_unreadable=unreadable,
    )


def installed_versions(distributions: Iterable[str]) -> tuple[tuple[str, str], ...]:
    """``(distribution, version)`` from package metadata; ``not_installed`` when absent."""

    found: list[tuple[str, str]] = []
    for name in distributions:
        try:
            found.append((name, metadata.version(name)))
        except metadata.PackageNotFoundError:
            found.append((name, "not_installed"))
    return tuple(found)


# --------------------------------------------------------------------------- #
# field allowlist and sanitizers
# --------------------------------------------------------------------------- #

FieldKind = Literal["id", "enum", "count", "time"]

#: The only record fields a bundle emits, per section, with the shape each must have.
#: ``has_error`` is derived; free text never appears here.
RECORD_FIELDS: Final[Mapping[str, tuple[tuple[str, FieldKind], ...]]] = {
    "snapshots": (
        ("review_id", "id"),
        ("repo_id", "id"),
        ("status", "enum"),
        ("snapshot_id", "id"),
        ("snapshot_number", "count"),
        ("capture_kind", "enum"),
        ("packet_schema_version", "count"),
        ("snapshot_created_at", "time"),
        ("review_updated_at", "time"),
    ),
    "intelligence_jobs": (
        ("job_id", "id"),
        ("kind", "enum"),
        ("review_id", "id"),
        ("repo_id", "id"),
        ("snapshot_id", "id"),
        ("capability", "enum"),
        ("driver", "enum"),
        ("state", "enum"),
        ("model_calls", "count"),
        ("created_at", "time"),
        ("completed_at", "time"),
    ),
    "queue_jobs": (
        ("job_id", "id"),
        ("repo_id", "id"),
        ("review_id", "id"),
        ("snapshot_id", "id"),
        ("capability", "enum"),
        ("execution_class", "enum"),
        ("execution_locus", "enum"),
        ("state", "enum"),
        ("attempt", "count"),
        ("retry_count", "count"),
        ("reclaim_count", "count"),
        ("publication_state", "enum"),
        ("created_at", "time"),
        ("completed_at", "time"),
    ),
    "provider_operations": (
        ("operation_id", "id"),
        ("review_id", "id"),
        ("repo_id", "id"),
        ("snapshot_id", "id"),
        ("provider", "enum"),
        ("kind", "enum"),
        ("state", "enum"),
        ("attempts", "count"),
        ("remote_ref", "id"),
        ("created_at", "time"),
        ("updated_at", "time"),
    ),
    "webhook_deliveries": (
        ("delivery_id", "id"),
        ("repo_id", "id"),
        ("review_id", "id"),
        ("provider", "enum"),
        ("event_kind", "enum"),
        ("state", "enum"),
        ("attempts", "count"),
        ("created_at", "time"),
        ("failed_at", "time"),
    ),
}
#: Newest-first ordering key per record section.
_SORT_FIELD: Final[Mapping[str, str]] = {
    "snapshots": "review_updated_at",
    "intelligence_jobs": "created_at",
    "queue_jobs": "created_at",
    "provider_operations": "updated_at",
    "webhook_deliveries": "created_at",
}

_ID_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_ENUM_RE: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9_.:-]{0,47}")
_VERSION_RE: Final[re.Pattern[str]] = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,39}")
_DATE_RE: Final[re.Pattern[str]] = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME_RE: Final[re.Pattern[str]] = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?)?(?:Z|[+-]\d{2}:?\d{2})?"
)
#: A data-flow setting name: ``LEMONCROW_LLM_BACKEND``, ``lc --root``, ``review policy run_plan.x``.
_SETTING_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_.:/\-\[\] ]{1,120}")
_MAX_COUNT: Final[int] = 10**12


def _credential_free(value: str) -> bool:
    """Whether the shared secret scrubbers leave *value* untouched (no key, token or JWT shape)."""

    from lemoncrow_client.kit.redaction import redact

    from lemoncrow.core.service.telemetry.scrubber import scrub_string

    return scrub_string(redact(value)) == value


def _has_opaque_run(value: str) -> bool:
    return any(_mask_opaque(match) != match.group() for match in _OPAQUE_RUN_RE.finditer(value))


def _opaque_id(value: object) -> str:
    """An identifier; one that is not id-shaped or is shaped like a credential is withheld."""

    if not isinstance(value, str) or not value:
        return ""
    return value if _ID_RE.fullmatch(value) and _credential_free(value) else WITHHELD


def _enum(value: object) -> str:
    """A short lower-case vocabulary word; anything else (including key or digest shapes) is ``other``."""

    if not isinstance(value, str) or not value:
        return ""
    if _ENUM_RE.fullmatch(value) and not _has_opaque_run(value) and _credential_free(value):
        return value
    return "other"


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= _MAX_COUNT else None


def _time(value: object) -> str:
    """A UTC ISO-8601 timestamp re-rendered from a parsed instant; anything else is ``""``."""

    moment: datetime
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int | float):
        if not 0 <= float(value) < 1e11:
            return ""
        moment = datetime.fromtimestamp(float(value), UTC)
    elif isinstance(value, str):
        text = value.strip()
        if not text or len(text) > 40 or not _TIME_RE.fullmatch(text):
            return ""
        try:
            moment = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        except ValueError:
            return ""
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
    else:
        return ""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _date(value: object) -> str:
    return value if isinstance(value, str) and _DATE_RE.fullmatch(value) else ""


def _version(value: object) -> str:
    if not isinstance(value, str) or not value:
        return ""
    return value if _VERSION_RE.fullmatch(value) else WITHHELD


def _setting(value: object) -> str:
    """A data-flow setting name; one carrying a credential shape or an opaque run is withheld."""

    if not isinstance(value, str) or not value:
        return ""
    if _SETTING_RE.fullmatch(value) and not _has_opaque_run(value) and _credential_free(value):
        return value
    return WITHHELD


def _flow_row_id(value: object) -> str:
    """A data-flow row id (``models.endpoint.<name>``): vocabulary, so an opaque run is withheld too."""

    ident = _opaque_id(value)
    return WITHHELD if ident and _has_opaque_run(ident) else ident


_SANITIZERS: Final[Mapping[str, Any]] = {"id": _opaque_id, "enum": _enum, "count": _count, "time": _time}


def _record_wire(section: str, record: object) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, kind in RECORD_FIELDS[section]:
        out[name] = _SANITIZERS[kind](getattr(record, name, None))
    out["has_error"] = bool(_errors_of(record))
    return out


# --------------------------------------------------------------------------- #
# the one redaction pass for free text
# --------------------------------------------------------------------------- #

#: Words that survive :func:`redact_text`. Diagnostic and Review vocabulary only;
#: a word is added here deliberately, never derived from data.
LEXICON: Final[frozenset[str]] = frozenset("""
    a an the and or not no none nor is are was were be been being has have had for from to of in on at by
    with without into onto than then after before while during per via as if when but only all any each
    every more less too many much one two three this that these those it its can cannot could would should
    must may might did does do done still yet already again out over under up down off also same other new
    old first last next previous both either neither some most least
    error errors exception failed failure fails fail refused refuse refuses denied deny denies forbidden
    unauthorized unauthenticated authentication authorization auth permission permissions invalid missing
    unknown unsupported unavailable available unreachable reachable timeout timed time times expired
    expiry exceeded exceeds exceed limit limits limited quota rate budget cancelled canceled cancel aborted
    abort interrupted killed crashed crash panic retry retries retrying retryable retried attempt attempts
    conflict conflicts stale busy locked lock lease leased reclaimed reclaim pending queued queue running
    completed complete succeeded success skipped skip ignored disabled enabled required requires require
    expected unexpected mismatch match matches matched differs different changed unchanged empty truncated
    partial incomplete large larger small smaller size bytes byte kib mib gib kb mb gb s ms sec seconds
    second minutes minute hours hour days day status code http https response request requests
    connection connect connected reset closed close open broken pipe network egress ingress proxy dns host
    resolve resolved resolution tls ssl certificate handshake protocol version versions schema format
    parse parsed parsing decode decoded encoding json yaml utf read write written readable writable found
    exist exists file files directory path paths disk space memory process exit exited signal terminated
    sandbox policy policies trust trusted untrusted source content access allowed restricted blocked
    block internal server client service worker workers job jobs task tasks run runs ran runner model
    models provider providers driver drivers endpoint endpoints token tokens key keys credential
    credentials secret secrets configured configuration config setting settings value values option
    options field fields type types name names id ids limit unit units target targets reference
    references ref refs
    review reviews snapshot snapshots revision revisions capture captured change changes diff hunk hunks
    patch commit commits branch base head merge merged rebase tree working staged index indexed impact
    sites site symbol symbols relations relation outline caller callers line lines ranges range
    definition definitions non centrality cross repository repositories absent ambiguous counts count
    astgrep intent add added excluded undetermined lemoncrow store submodule submodules dirty symlink
    mode unmerged provenance dependency side analysis analyzed record records digest check checks
    browser render runtime observation validation differential behavior delta duplicate resolve
    counted register evidence brief ask intelligence synthesis plan ai llm prompt webhook webhooks
    delivery deliveries publish publication published comment comments suggestion approval approve
    judgment landing deployment deployments observability git github gitlab bitbucket pull issue sync
    checkout current generation reuse cache cached hermetic executor locus managed customer local
    hosted organization contract contracts verification verified waiver feedback annotation
    annotations author agent session sessions degraded reason reasons
    openai anthropic litellm ollama claude codex gemini opencode
    """.split())

_RUN_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9]+|[^A-Za-z0-9]+")
_ASCII_ALNUM_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9]")
_TRAILING_ALNUM_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9]+\Z")
_TRAILING_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"\S+\Z")
_WORD_PART_RE: Final[re.Pattern[str]] = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])")
_KEPT_PUNCTUATION: Final[frozenset[str]] = frozenset(" _-.:,;()/=[]'\"")
_COLLAPSE_RE: Final[re.Pattern[str]] = re.compile(r"…(?:[ _\-.:,;()/=\[\]'\"]*…)+")
_MAX_KEPT_DIGITS: Final[int] = 3


def redact_text(text: str, *, limit: int = MAX_TEXT_CHARS) -> str:
    """The one redaction pass for free text: lexicon words, short numbers and punctuation survive.

    A run of letters and digits mixed (keys, ids, digests) becomes one ``…``
    whole; a letters-only run keeps its lexicon words (camelCase split); a
    digits-only run of at most three digits survives. Every other run
    (identifiers, paths, hosts, values, source text, model output) becomes
    ``…``; consecutive placeholders collapse into one. The result is
    lower-cased, whitespace-normalized and at most *limit* characters.
    """

    pieces: list[str] = []
    head = text[:MAX_TEXT_INPUT_CHARS]
    if len(text) > MAX_TEXT_INPUT_CHARS and _ASCII_ALNUM_RE.match(text, MAX_TEXT_INPUT_CHARS):
        # The cap splits a letters-and-digits run: drop its head, so a cut key or long number
        # cannot pass as a lexicon word or a short number.
        head = _TRAILING_ALNUM_RE.sub("", head)
    for match in _RUN_RE.finditer(head):
        run = match.group()
        if run.isascii() and run.isalpha():
            for part in _WORD_PART_RE.findall(run):
                word = part.lower()
                pieces.append(word if word in LEXICON else PLACEHOLDER)
        elif run.isascii() and run.isdigit():
            pieces.append(run if len(run) <= _MAX_KEPT_DIGITS else PLACEHOLDER)
        elif run[0].isascii() and run[0].isalnum():
            pieces.append(PLACEHOLDER)
        else:
            pieces.append("".join(char if char in _KEPT_PUNCTUATION else " " for char in run))
    joined = " ".join(_COLLAPSE_RE.sub(PLACEHOLDER, "".join(pieces)).split())
    if len(joined) > limit:
        joined = joined[: limit - 1].rstrip() + PLACEHOLDER
    return joined


_URL_USERINFO_RE: Final[re.Pattern[str]] = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,15}://)[^\s/@]*@")
_URL_QUERY_RE: Final[re.Pattern[str]] = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]{1,15}://[^\s?#]*)\?[^\s#]*")
_AUTH_SCHEME_RE: Final[re.Pattern[str]] = re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{6,}")
_COOKIE_RE: Final[re.Pattern[str]] = re.compile(r"(?i)\b(set-cookie|cookie)\s*[:=]\s*[^\r\n]+")
#: An unbroken letters-and-digits run this long is a key, token or digest, never prose
#: or a model id (whose segments are short): ``sk-…``/``xoxb-…`` bodies, hex digests, base64.
#: Unicode letters and digits count, so a full-width or non-Latin key is a run too.
_OPAQUE_RUN_RE: Final[re.Pattern[str]] = re.compile(r"[^\W_]{16,}")


def _mask_opaque(match: re.Match[str]) -> str:
    run = match.group()
    if any(char.isdigit() for char in run) and any(char.isalpha() for char in run):
        return "<token>"
    return run


def _shape_scrub(text: str) -> str:
    """URL credentials and query strings, auth-scheme credentials, secret shapes and opaque runs removed.

    NFKC first, so a full-width spelling of a key meets the same patterns as its ASCII form.
    """

    from lemoncrow_client.kit.redaction import redact

    from lemoncrow.core.service.telemetry.scrubber import scrub_string

    out = _URL_USERINFO_RE.sub(r"\1<credentials>@", unicodedata.normalize("NFKC", text))
    out = _URL_QUERY_RE.sub(r"\1?<query>", out)
    out = _AUTH_SCHEME_RE.sub(r"\1 <credential>", out)
    out = _COOKIE_RE.sub(r"\1: <cookie>", out)
    return _OPAQUE_RUN_RE.sub(_mask_opaque, scrub_string(redact(out)))


#: What a masked secret becomes before the lexicon pass: a letter-and-digit run, which
#: :func:`redact_text` turns into ``…`` (never a vocabulary word that could pass as prose).
_SECRET_MARK: Final[str] = "q0"


def _masked(raw: str, guard: SecretGuard) -> str:
    """*raw* with every configured secret replaced by an opaque mark, before the lexicon pass.

    The lexicon pass keeps ordinary words, so a secret made of them (``review-token-cache-lock``)
    would survive it. Escapes are decoded and only the first ``MAX_TEXT_INPUT_CHARS`` are read
    (what :func:`redact_text` reads); the match is the same whole-value match as the error-text scrub.
    """

    if not guard.labels or not raw:
        return raw
    window = decode_escapes(raw[:MAX_TEXT_INPUT_CHARS])
    key, origins = match_key(window)
    spans = _env_spans(window, key, origins, guard.labels, len(window) + 1)
    if not guard.tenant_visible:
        spans = _merge_spans(
            [*spans, *_fragment_spans(window, key, origins, guard.labels, guard.keys, len(window) + 1)]
        )
    if not spans:
        return raw
    pieces: list[str] = []
    pos = 0
    for start, end, _name in spans:
        pieces.append(window[pos:start])
        pieces.append(_SECRET_MARK)
        pos = end
    pieces.append(window[pos:])
    return "".join(pieces)


def _model_label(value: object, guard: SecretGuard = NO_SECRETS) -> str:
    """A plain model id for the ``model-names`` include, else ``<withheld>``.

    The id must pass the data-flow projection's allowlist (``shown_model_name``:
    ``[vendor/]name[:tag]``, no host, no credential shape) and must neither
    equal, contain nor be contained in a configured secret value.
    """

    if not clean_model_name(value):
        return ""
    name = shown_model_name(value)
    return name if name and _shape_scrub(name) == name and not guard.overlaps(name) else WITHHELD


#: Shortest prefix of a secret the cut removes when the secret continues past it.
_MIN_DANGLING_CHARS: Final[int] = 4


def _past_marks(window: str, end: int) -> int:
    """*end*, moved past the combining marks and zero-width characters that follow a match.

    The folded key drops them, so a span ends at its last kept character; the marks that
    decorate it would otherwise be left standing after the replacement.
    """

    while end < len(window) and unicodedata.category(window[end]) in {"Mn", "Me", "Cf"}:
        end += 1
    return end


def _env_spans(
    window: str, key: str, origins: Sequence[int], secrets: Mapping[str, str], cap: int
) -> list[tuple[int, int, str]]:
    """Every occurrence of a secret (in folded key space) that starts before *cap*, overlaps merged.

    *key* and *origins* are ``match_key(window)``: a secret a line break or a
    lookalike letter splits still matches, and the span is in *window* indices.
    """

    spans: list[tuple[int, int, str]] = []
    for needle, name in secrets.items():
        start = key.find(needle)
        while 0 <= start and origins[start] < cap:
            spans.append((origins[start], _past_marks(window, origins[start + len(needle) - 1] + 1), name))
            start = key.find(needle, start + 1)
    return _merge_spans(spans)


#: A secret of at least this many folded characters is removed by fragment, not only whole.
_FRAGMENT_MIN_SECRET_CHARS: Final[int] = 12
#: Length of the secret fragment that is removed wherever it appears (a secret split across fields).
_FRAGMENT_CHARS: Final[int] = 6


def _fragment_spans(
    window: str, key: str, origins: Sequence[int], secrets: Mapping[str, str], configured: Iterable[str], cap: int
) -> list[tuple[int, int, str]]:
    """Runs of *key* made of ``_FRAGMENT_CHARS``-character pieces of a configured secret.

    A secret split across fields or lines with text between the halves has no
    whole occurrence; each half is still a run of its pieces.
    """

    pieces: dict[str, str] = {}
    for secret in sorted(configured):
        if len(secret) >= _FRAGMENT_MIN_SECRET_CHARS:
            name = secrets.get(secret, "secret")
            for index in range(len(secret) - _FRAGMENT_CHARS + 1):
                pieces.setdefault(secret[index : index + _FRAGMENT_CHARS], name)
    runs: list[list[Any]] = []
    for index in range(len(key) - _FRAGMENT_CHARS + 1):
        owner = pieces.get(key[index : index + _FRAGMENT_CHARS])
        if owner is None:
            continue
        if runs and index < runs[-1][1]:
            runs[-1][1] = index + _FRAGMENT_CHARS
        else:
            runs.append([index, index + _FRAGMENT_CHARS, owner])
    return [
        (origins[first], _past_marks(window, origins[last - 1] + 1), name)
        for first, last, name in runs
        if origins[first] < cap
    ]


def _merge_spans(spans: Iterable[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    merged: list[tuple[int, int, str]] = []
    for start, end, name in sorted(spans):
        if merged and start < merged[-1][1]:
            first_start, first_end, first_name = merged[-1]
            merged[-1] = (first_start, max(first_end, end), first_name)
        else:
            merged.append((start, end, name))
    return merged


def _dangling_start(key: str, origins: Sequence[int], secrets: Mapping[str, str], cut: int) -> int:
    """Where text before *cut* begins a secret that continues past it, else *cut*.

    The cut can fall inside a secret a long gap separates; what precedes it is
    then a prefix of that secret and is dropped with it.
    """

    before = bisect_left(origins, cut)
    start = cut
    for needle in secrets:
        for length in range(min(len(needle) - 1, before), _MIN_DANGLING_CHARS - 1, -1):
            if key[before - length : before] == needle[:length]:
                start = min(start, origins[before - length])
                break
    return start


def scrub_included_text(
    text: str,
    *,
    environ: Mapping[str, str] | None = None,
    guard: SecretGuard | None = None,
    env_names: bool = True,
) -> str:
    """Error text for the ``error-text`` include: bounded, with credentials and environment values removed.

    Removed, in order: every configured secret (*guard*: the values of
    secret-named environment variables, replaced by the variable's name,
    URL passwords and secret-named query values, and the caller's extras; a
    value of a variable whose name is not secret-like is not a secret, and a
    local secret of 12+ characters is also removed by six-character fragment,
    so halves a caller joined with other text between them do not survive),
    URL credentials and query strings, ``Bearer``/``Basic``/``token``
    credentials, ``Cookie`` and ``Set-Cookie`` values, the shared secret and
    PII scrubbers (API-key, token, JWT and private-key shapes, e-mail
    addresses, IP addresses, repository URLs and local paths), then every
    unbroken run of 16+ letters and digits that mixes both (unknown key
    shapes, digests). A *tenant_visible* guard removes only whole values: no
    fragment or cut-prefix match, so the output cannot be used to test guesses
    against a server secret.

    ``%XX`` and ``\\uXXXX`` escapes are decoded and the text is NFKC-normalized
    first, and secrets are found in :func:`match_key` space, so a full-width
    spelling, an accented or lookalike letter, a combining mark, a zero-width
    character or a line break inside a value does not hide it.
    At most ``4 * MAX_INCLUDED_TEXT_CHARS`` normalized characters are kept, and
    the read is bounded before and after normalization (which can grow or
    shrink text). Secrets are found before that cut, so one that straddles it
    is replaced whole, and what precedes the cut of a secret that continues
    past it is dropped; a token the cut splits is dropped, so no shape pattern
    sees half a credential. With ``env_names=False`` a removed value is labelled
    ``<env:secret>``, so an operator's variable names never reach the reader.
    """

    env = os.environ if environ is None else environ
    configured = guard if guard is not None else SecretGuard.from_environ(env)
    secrets: dict[str, str] = dict(configured.labels)
    longest = max((len(folded) for folded in secrets), default=0)
    cap = MAX_INCLUDED_TEXT_CHARS * 4
    # Bound the raw read, normalize, bound again: the window reaches past the cut by the longest value.
    limit = cap + longest + 1
    normalized = unicodedata.normalize("NFKC", decode_escapes(text[:limit]))
    # NFKC can expand a character up to 18x; the second bound only caps the env scan, cut stays <= cap.
    window = normalized[:limit]
    # When the raw read stopped early, keep `longest` characters after the cut so a value that starts
    # before it is found whole (a shrinking normalization can leave the window shorter than the cap).
    more_follows = len(text) > limit
    cut = max(0, min(cap, len(window) - longest - 1 if more_follows else len(window)))
    key, origins = match_key(window)
    found = _env_spans(window, key, origins, secrets, cut)
    if not configured.tenant_visible:
        found = [*found, *_fragment_spans(window, key, origins, secrets, configured.keys, cut)]
    spans = _merge_spans(found)
    if (more_follows or cut < len(window)) and not configured.tenant_visible:
        cut = min(cut, _dangling_start(key, origins, secrets, cut))
    pieces: list[str] = []
    pos = 0
    for start, end, name in spans:
        pieces.append(window[pos:start])
        pieces.append(f"<env:{name if env_names else 'secret'}>")
        pos = end
    if pos < cut:
        tail = window[pos:cut]
        if cut < len(window) and not window[cut].isspace():
            tail = _TRAILING_TOKEN_RE.sub("", tail)
        pieces.append(tail)
    out = " ".join(_shape_scrub("".join(pieces)).split())
    if len(out) > MAX_INCLUDED_TEXT_CHARS:
        out = out[: MAX_INCLUDED_TEXT_CHARS - 1] + PLACEHOLDER
    return out


# --------------------------------------------------------------------------- #
# builder
# --------------------------------------------------------------------------- #


def require_includes(requested: Iterable[str], allowed: Sequence[str]) -> tuple[str, ...]:
    """The requested include names, deduplicated and sorted; any name *allowed* lacks is refused."""

    names = sorted({str(name).strip() for name in requested})
    for name in names:
        if name not in allowed:
            shown = name[:64]
            choices = ", ".join(allowed) if allowed else "none on this surface"
            raise ValueError(f"unknown include {shown!r}; allowed: {choices}")
    return tuple(names)


def _unavailable(reason: str) -> dict[str, Any]:
    if reason not in UNAVAILABLE_REASONS:
        raise ValueError(f"unregistered unavailable reason {reason!r}")
    return {"status": "unavailable", "reason": reason, "detail": UNAVAILABLE_REASONS[reason]}


def _unknown(reason: str) -> dict[str, Any]:
    """A state nothing reports yet: not unavailable (nothing failed), not populated (nothing is claimed)."""

    if reason not in UNAVAILABLE_REASONS:
        raise ValueError(f"unregistered unavailable reason {reason!r}")
    return {"status": "unknown", "reason": reason, "detail": UNAVAILABLE_REASONS[reason]}


def _note(part: str, reason: str) -> dict[str, str]:
    if reason not in UNAVAILABLE_REASONS:
        raise ValueError(f"unregistered unavailable reason {reason!r}")
    return {"part": part, "reason": reason, "detail": UNAVAILABLE_REASONS[reason]}


def _errors_of(record: object) -> tuple[tuple[str, str], ...]:
    """``(source, raw text)`` of every error a record carries."""

    found: list[tuple[str, str]] = []
    error = getattr(record, "error", "")
    if isinstance(error, str) and error.strip():
        source = {
            IntelligenceJobRecord: "intelligence_job",
            QueueJobRecord: "queue_job",
            ProviderOperationRecord: "provider_operation",
            WebhookDeliveryRecord: "webhook_delivery",
        }.get(type(record), "other")
        found.append((source, error))
    publication = getattr(record, "publication_error", "")
    if isinstance(publication, str) and publication.strip():
        found.append(("queue_publication", publication))
    return tuple(found)


def _record_ref(record: object) -> tuple[str, object]:
    for attr in ("job_id", "operation_id", "delivery_id"):
        value = getattr(record, attr, None)
        if isinstance(value, str):
            when = next(
                (
                    getattr(record, name)
                    for name in ("failed_at", "completed_at", "updated_at", "created_at")
                    if getattr(record, name, None)
                ),
                "",
            )
            return value, when
    return "", ""


def _sorted_records(section: str, records: Sequence[Any]) -> list[Any]:
    key = _SORT_FIELD[section]
    return sorted(
        records,
        key=lambda record: (_time(getattr(record, key, None)), str(getattr(record, RECORD_FIELDS[section][0][0], ""))),
        reverse=True,
    )


def _record_section(
    section: str, source: RecordSource, limit: int, extra: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    if source.unavailable:
        return _unavailable(source.unavailable)
    ordered = _sorted_records(section, source.records)
    shown = ordered[:limit]
    records = []
    for record in shown:
        wire = _record_wire(section, record)
        for name, render in (extra or {}).items():
            wire[name] = render(record)
        records.append(wire)
    return {
        "status": "populated",
        "records": records,
        "limit": limit,
        "truncated": bool(source.truncated or len(ordered) > limit),
    }


def _versions_section(facts: VersionFacts) -> dict[str, Any]:
    from lemoncrow.pro.capabilities.review.models import SCHEMA_VERSION as PACKET_SCHEMA_VERSION
    from lemoncrow.pro.capabilities.review.session_models import SESSION_SCHEMA_VERSION

    return {
        "status": "populated",
        "components": {_enum(name) or WITHHELD: _version(version) for name, version in facts.components},
        "runtime": {"python": _version(platform.python_version()), "os": _enum(platform.system().lower())},
        "schemas": {
            "bundle": BUNDLE_SCHEMA_VERSION,
            "data_flow": DATA_FLOW_SCHEMA_VERSION,
            "review_packet": PACKET_SCHEMA_VERSION,
            "review_session": SESSION_SCHEMA_VERSION,
            "redaction_lexicon": LEXICON_VERSION,
        },
        "store_schemas": sorted({_enum(marker) or WITHHELD for marker in facts.store_schemas}),
    }


_CAPTURE_MODES: Final[frozenset[str]] = frozenset({"derived", "packet"})
_MANIFEST_SCHEMAS: Final[tuple[str, ...]] = ("packet", "evidence", "policy", "record")
_MANIFEST_KEYS: Final[frozenset[str]] = frozenset(
    {"manifest", "schemas", "evidence_kinds", "capture_modes", "capture_options"}
)


def _names(value: object) -> set[str]:
    if not isinstance(value, list | tuple):
        return set()
    return {item for item in value if isinstance(item, str)}


def _manifest(review: Mapping[str, Any]) -> dict[str, Any]:
    """The Review manifest, restricted to the names this build knows."""

    from lemoncrow_client.protocol import CAPTURE_OPTIONS, REVIEW_FEATURES

    from lemoncrow.pro.capabilities.review.session_models import REVIEW_EVIDENCE_KINDS

    schemas: dict[str, list[int]] = {}
    raw_schemas = review.get("schemas")
    if isinstance(raw_schemas, Mapping):
        for name in _MANIFEST_SCHEMAS:
            values = raw_schemas.get(name)
            if isinstance(values, list):
                schemas[name] = sorted({value for value in values if _count(value) is not None})[:8]
    known = set(REVIEW_FEATURES) | _MANIFEST_KEYS
    return {
        "manifest": _count(review.get("manifest")),
        "features": {name: review[name] for name in REVIEW_FEATURES if isinstance(review.get(name), bool)},
        "unknown_features": sum(1 for key in review if key not in known),
        "schemas": schemas,
        "evidence_kinds": sorted(_names(review.get("evidence_kinds")) & set(REVIEW_EVIDENCE_KINDS)),
        "capture_modes": sorted(_names(review.get("capture_modes")) & _CAPTURE_MODES),
        "capture_options": sorted(_names(review.get("capture_options")) & CAPTURE_OPTIONS),
    }


def _managed_worker_protocol(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    compatible = value.get("compatible")
    answer: dict[str, Any] = {"compatible": compatible if isinstance(compatible, bool) else None}
    for name in ("negotiated_protocol", "worker_protocol"):
        answer[name] = _date(value.get(name))
    supported = value.get("worker_supported_protocols")
    answer["worker_supported_protocols"] = [
        parsed for item in (supported if isinstance(supported, list | tuple) else ())[:8] if (parsed := _date(item))
    ]
    legacy = value.get("legacy_n_minus_one")
    answer["legacy_n_minus_one"] = legacy if isinstance(legacy, bool) else False
    side = str(value.get("side_to_upgrade") or "")
    answer["side_to_upgrade"] = side if side in {"controller", "worker", "controller_and_worker", "unknown"} else ""
    reason = value.get("reason")
    answer["reason"] = redact_text(str(reason)) if isinstance(reason, str) and reason else ""
    return answer


def _protocol_section(facts: ProtocolFacts) -> dict[str, Any]:
    notes: list[dict[str, str]] = []
    client: dict[str, str] | None = None
    if facts.client_window is not None:
        current, previous = facts.client_window
        client = {"protocol_version": _date(current), "previous_protocol_version": _date(previous)}
    else:
        notes.append(_note("client", facts.client_reason or "not_applicable_hosted"))
    server: dict[str, Any] | None = None
    manifest: dict[str, Any] | None = None
    identity = facts.server_identity
    if isinstance(identity, Mapping):
        supported = [_date(value) for value in list(identity.get("supported_protocol_versions") or [])[:8]]
        deployment = identity.get("deployment")
        mode = deployment.get("mode") if isinstance(deployment, Mapping) else ""
        server = {
            "name": _enum(identity.get("name")),
            "protocol_version": _date(identity.get("protocol_version")),
            "supported_protocol_versions": [value for value in supported if value],
            "deployment_mode": _enum(mode),
        }
        products = identity.get("product_capabilities")
        review = products.get("review") if isinstance(products, Mapping) else None
        if isinstance(review, Mapping):
            manifest = _manifest(review)
        else:
            notes.append(_note("review_manifest", "no_server_manifest"))
    else:
        notes.append(_note("server", facts.server_reason or "server_unreachable"))
    worker = _managed_worker_protocol(facts.managed_worker)
    if worker is None and facts.worker_reason:
        notes.append(_note("managed_worker", facts.worker_reason))
    if client is None and server is None:
        return {**_unavailable(facts.server_reason or "server_unreachable"), "notes": notes}
    compatible: bool | None = None
    if client is not None and server is not None:
        offered = {client["protocol_version"], client["previous_protocol_version"]} - {""}
        compatible = bool(offered & set(server["supported_protocol_versions"]))
    return {
        "status": "populated",
        "client": client,
        "server": server,
        "compatible": compatible,
        "review_manifest": manifest,
        "managed_worker": worker,
        "notes": notes,
    }


_CATEGORY_IDS: Final[frozenset[str]] = frozenset(category for category, _title in CATEGORIES)


_NAME_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"[.\s]+")


def _unsecret(value: str, guard: SecretGuard) -> str:
    """*value*, or ``<withheld>`` when its final name segment overlaps a configured secret.

    Ids and settings such as ``models.endpoint.<name>`` and ``providers.json endpoints.<name>``
    end in an operator-chosen name; only that name is tested. The vocabulary in front of it
    (``review policy run_plan.require``) is fixed by the product and is never a secret.
    """

    if not value or value == WITHHELD:
        return value
    return WITHHELD if guard.overlaps(_NAME_SPLIT_RE.split(value)[-1]) else value


def _flow_wire(label: str, projection: DataFlowProjection, guard: SecretGuard = NO_SECRETS) -> dict[str, Any]:
    """Data-flow *origins*: row id, category, status and the setting/layer each came from."""

    rows = []
    summary: dict[str, int] = {}
    for row in projection.rows:
        status = row.status if row.status in FLOW_STATUSES else "unknown"
        summary[status] = summary.get(status, 0) + 1
        rows.append(
            {
                "id": _unsecret(_flow_row_id(row.id), guard),
                "category": row.category if row.category in _CATEGORY_IDS else "other",
                "status": status,
                "origins": [
                    {"setting": _unsecret(_setting(origin.setting), guard), "layer": _enum(origin.layer)}
                    for origin in row.origins
                ],
            }
        )
    return {
        "repository": _unsecret(_opaque_id(label), guard),
        "surface": _enum(projection.surface),
        "rows": rows,
        "summary": summary,
    }


def _data_flow_section(facts: DataFlowFacts, limit: int, guard: SecretGuard = NO_SECRETS) -> dict[str, Any]:
    if facts.unavailable:
        return _unavailable(facts.unavailable)
    if not facts.projections:
        return _unavailable("no_repositories")
    return {
        "status": "populated",
        "schema_version": DATA_FLOW_SCHEMA_VERSION,
        "repositories": [_flow_wire(label, projection, guard) for label, projection in facts.projections[:limit]],
        "limit": limit,
        "truncated": bool(facts.truncated or len(facts.projections) > limit),
    }


_LOCUS_WORDS: Final[frozenset[str]] = frozenset(
    {"local", "managed", "customer_worker", "external", "customer_local", "unknown"}
)
_LOCUS_PREFIXES: Final[tuple[str, ...]] = ("execution.locus.", "models.locus.")


def _execution_locus_section(facts: DataFlowFacts, limit: int, guard: SecretGuard = NO_SECRETS) -> dict[str, Any]:
    """Where review work runs and where model data goes, read from the data-flow projection (one locus model)."""

    if facts.unavailable:
        return _unavailable(facts.unavailable)
    if not facts.projections:
        return _unavailable("no_repositories")
    repositories = []
    for label, projection in facts.projections[:limit]:
        rows = []
        for row in projection.rows:
            if not row.id.startswith(_LOCUS_PREFIXES):
                continue
            status = row.status if row.status in FLOW_STATUSES else "unknown"
            words = sorted({word for word in row.locus if word in _LOCUS_WORDS})
            rows.append(
                {
                    "id": _unsecret(_flow_row_id(row.id), guard),
                    "kind": "execution" if row.id.startswith("execution.") else "model",
                    "status": status,
                    "locus": words,
                }
            )
        repositories.append({"repository": _unsecret(_opaque_id(label), guard), "rows": rows})
    return {
        "status": "populated",
        "repositories": repositories,
        "limit": limit,
        "truncated": bool(facts.truncated or len(facts.projections) > limit),
    }


def _release_wire(release: ReleaseState) -> dict[str, Any]:
    if release.unknown:
        if release.unknown not in UNAVAILABLE_REASONS:
            raise ValueError(f"unregistered unavailable reason {release.unknown!r}")
        return {"state": "unknown", "reason": release.unknown, "detail": UNAVAILABLE_REASONS[release.unknown]}
    return {"state": _enum(release.state) or "unknown"}


def _gates_section(operations: OperationsFacts) -> dict[str, Any]:
    release = _release_wire(operations.release)
    if not operations.gates:
        return _unknown("gates_not_reported")
    gates = []
    for gate in operations.gates:
        row: dict[str, Any] = {"gate": _enum(gate.gate)}
        if gate.unreadable:
            row.update(status="unknown", reason="gate_unreadable", detail=UNAVAILABLE_REASONS["gate_unreadable"])
        else:
            row.update(
                status="populated",
                mode=_enum(gate.mode),
                thresholds_approved=gate.approved if isinstance(gate.approved, bool) else None,
            )
            if gate.baseline_decision or gate.baseline_recorded_at:
                row["baseline"] = {
                    "decision": _enum(gate.baseline_decision),
                    "recorded_at": _time(gate.baseline_recorded_at),
                }
        gates.append(row)
    return {"status": "populated", "gates": gates, "release": release}


def _reported_section(state: ReportedState) -> dict[str, Any]:
    if state.unknown:
        return _unknown(state.unknown)
    return {
        "status": "populated",
        "counts": {_enum(name): count for name, value in state.counts[:32] if (count := _count(value)) is not None},
        "states": {_enum(name): _enum(word) for name, word in state.words[:32]},
    }


def _degradations_section(
    source: RecordSource, limits: DiagnosticLimits, guard: SecretGuard = NO_SECRETS
) -> dict[str, Any]:
    if source.unavailable:
        return _unavailable("derived_unavailable")
    degraded = [record for record in _sorted_records("snapshots", source.records) if record.degraded]
    records = []
    totals: dict[str, int] = {}
    for record in degraded[: limits.degradations]:
        reasons = [redact_text(_masked(reason, guard), limit=MAX_REASON_CHARS) for reason in record.degraded]
        for reason in reasons:
            totals[reason] = totals.get(reason, 0) + 1
        records.append(
            {
                "review_id": _opaque_id(record.review_id),
                "repo_id": _opaque_id(record.repo_id),
                "snapshot_id": _opaque_id(record.snapshot_id),
                "reasons": reasons[: limits.reasons_per_snapshot],
                "reasons_total": len(reasons),
            }
        )
    return {
        "status": "populated",
        "records": records,
        "by_reason": dict(sorted(totals.items(), key=lambda item: (-item[1], item[0]))[: limits.errors]),
        "limit": limits.degradations,
        "truncated": len(degraded) > limits.degradations,
    }


def _errors_section(
    sources: Sequence[RecordSource],
    *,
    limit: int,
    salt: bytes,
    error_text: bool,
    environ: Mapping[str, str] | None,
    guard: SecretGuard,
    env_names: bool,
) -> dict[str, Any]:
    if all(source.unavailable for source in sources):
        return _unavailable("derived_unavailable")
    found: list[tuple[str, str, str, str, str]] = []
    for source in sources:
        if source.unavailable:
            continue
        for record in source.records:
            ref, when = _record_ref(record)
            for origin, raw in _errors_of(record):
                found.append((_time(when), origin, ref, str(getattr(record, "repo_id", "")), raw))
    found.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    records = []
    for when, origin, ref, repo_id, raw in found[:limit]:
        entry: dict[str, Any] = {
            "source": origin,
            "ref": _opaque_id(ref),
            "repo_id": _opaque_id(repo_id),
            "at": when,
            "message": redact_text(_masked(raw, guard)),
            "fingerprint": hmac.new(salt, raw.encode("utf-8", "replace"), hashlib.sha256).hexdigest()[:12],
        }
        if error_text:
            entry["text"] = scrub_included_text(raw, environ=environ, guard=guard, env_names=env_names)
        records.append(entry)
    return {"status": "populated", "records": records, "limit": limit, "truncated": len(found) > limit}


def build_diagnostic_bundle(
    facts: DiagnosticFacts,
    *,
    include: Iterable[str] = (),
    allowed_includes: Sequence[str],
    requested_by: str,
    now: datetime | None = None,
    limits: DiagnosticLimits = DEFAULT_LIMITS,
    salt: bytes | None = None,
    environ: Mapping[str, str] | None = None,
    extra_secrets: Iterable[str] = (),
    env_names: bool = True,
    tenant_visible: bool = False,
) -> dict[str, Any]:
    """The bundle document. Raises ``ValueError`` for an include *allowed_includes* lacks.

    *env_names* is whether included error text names the environment variable
    whose value it removed; a hosted bundle sets it false. *tenant_visible* is
    true when someone other than the operator reads the bundle (hosted): secrets
    are then matched whole, with eight-character name overlap floors, so the
    output cannot be used to test guesses against a server secret.

    *extra_secrets* are configured secret values beyond the process environment's
    (which is *environ*, default ``os.environ``); none of them, nor anything
    containing or contained in one, may appear in a model name, endpoint name
    or included error text.
    """

    included = require_includes(include, allowed_includes)
    moment = _time((now or datetime.now(UTC)).isoformat())
    key = salt if salt is not None else secrets.token_bytes(16)
    records = facts.records
    guard = SecretGuard.from_environ(
        os.environ if environ is None else environ, extra_secrets, tenant_visible=tenant_visible
    )
    env_names = env_names and not tenant_visible
    model_names = INCLUDE_MODEL_NAMES in included
    jobs_extra = {"model": lambda record: _model_label(record.model, guard)} if model_names else None
    sections: dict[str, dict[str, Any]] = {
        "versions": _versions_section(facts.versions),
        "protocol": _protocol_section(facts.protocol),
        "data_flow": _data_flow_section(facts.data_flow, limits.data_flow_repositories, guard),
        "execution_locus": _execution_locus_section(facts.data_flow, limits.data_flow_repositories, guard),
        "gates": _gates_section(facts.operations),
        "snapshots": _record_section("snapshots", records.snapshots, limits.snapshots),
        "intelligence_jobs": _record_section("intelligence_jobs", records.intelligence_jobs, limits.jobs, jobs_extra),
        "queue_jobs": _record_section("queue_jobs", facts.queue_jobs, limits.jobs),
        "job_lifecycle": _reported_section(facts.operations.job_lifecycle),
        "background_budget": _reported_section(facts.operations.background_budget),
        "provider_operations": _record_section("provider_operations", records.provider_operations, limits.operations),
        "webhook_deliveries": _record_section("webhook_deliveries", records.webhook_deliveries, limits.webhooks),
        "degradations": _degradations_section(records.snapshots, limits, guard),
        "recent_errors": _errors_section(
            (records.intelligence_jobs, facts.queue_jobs, records.provider_operations, records.webhook_deliveries),
            limit=limits.errors,
            salt=key,
            error_text=INCLUDE_ERROR_TEXT in included,
            environ=environ,
            guard=guard,
            env_names=env_names,
        ),
    }
    if tuple(sections) != SECTIONS:
        raise RuntimeError("a diagnostic bundle must carry every section, populated or unavailable")
    return {
        "schema": BUNDLE_SCHEMA,
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "surface": facts.surface,
        "generated_at": moment,
        "scope": {
            "organization": _opaque_id(facts.organization),
            "repository": _opaque_id(facts.repository),
            "repositories_total": max(facts.repositories_total, records.repositories_scanned),
            "repositories_scanned": records.repositories_scanned,
            "repositories_unreadable": records.repositories_unreadable,
        },
        "include": {
            "requested": list(included),
            "allowed": list(allowed_includes),
            "by": _opaque_id(requested_by),
            "at": moment,
            "adds": {name: INCLUDE_OPTIONS[name] for name in included},
        },
        "redaction": {
            "fields": "allowlist",
            "free_text": "lexicon",
            "lexicon_version": LEXICON_VERSION,
            "max_text_chars": MAX_TEXT_CHARS,
            "fingerprint": "HMAC-SHA256 under a key generated for this bundle; equal only within it",
        },
        "limits": limits.to_wire(),
        "sections": sections,
    }


__all__ = [
    "BUNDLE_SCHEMA",
    "BUNDLE_SCHEMA_VERSION",
    "DEFAULT_LIMITS",
    "HOSTED_INCLUDES",
    "INCLUDE_ERROR_TEXT",
    "INCLUDE_MODEL_NAMES",
    "INCLUDE_OPTIONS",
    "LEXICON",
    "LOCAL_INCLUDES",
    "RECORD_FIELDS",
    "SECTIONS",
    "UNAVAILABLE_REASONS",
    "DataFlowFacts",
    "DiagnosticFacts",
    "DiagnosticLimits",
    "IntelligenceJobRecord",
    "OperationsFacts",
    "ProtocolFacts",
    "ProviderOperationRecord",
    "QueueJobRecord",
    "RecordSource",
    "ReportedState",
    "ReviewRecords",
    "ReviewStoreReader",
    "SessionRow",
    "SnapshotRecord",
    "VersionFacts",
    "WebhookDeliveryRecord",
    "build_diagnostic_bundle",
    "collect_review_records",
    "installed_versions",
    "order_by_recency",
    "redact_text",
    "require_includes",
    "scrub_included_text",
]
