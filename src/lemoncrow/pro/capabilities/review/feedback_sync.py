"""Provider-published feedback: remote identity, sync and visible failure (Phase 12, P12-AUTO item 11).

A comment that was never published is LemonCrow's alone: editing or
withdrawing it stays local (the Reader's ordinary comment edit). A comment that
is on the provider has a remote identity (the ``github_review`` delivery's
``remote_ref``: ``github-comment:<id>`` for an inline review comment,
``github-review:<id>`` for a comment folded into the review body). Changing it
must reach the provider:

* inline comment: GitHub edit (``PATCH .../pulls/comments/<id>``) or delete
  (``DELETE .../pulls/comments/<id>``);
* folded comment: GitHub cannot edit one part of a review body, so the change
  is an auditable follow-up correction on the pull request;
* a published review verdict: GitHub dismissal (``PUT .../dismissals``).

The local copy changes only after the provider confirmed. A provider failure
is recorded as a visible ``sync_failed`` state with the exact error and the
request that failed; the local comment is not silently changed, and the human
retries or discards the request.

Durable state: ``review_derived`` rows of kind :data:`FEEDBACK_SYNC_ROW_KIND`,
one per annotation (or review verdict) under the Snapshot it was published on.
Writer: :class:`FeedbackSyncLedger`. Closer: the Review deletion cascade; a
discarded failure is closed as ``abandoned``.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, cast

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.session_models import Annotation, DeliveryRecord

FEEDBACK_SYNC_ROW_KIND: Final[str] = "feedback_sync"
_ROW_STAMP: Final[str] = "fs1"

SyncState = Literal["sync_pending", "synced", "corrected", "sync_failed", "abandoned"]
SYNC_STATES: Final[tuple[SyncState, ...]] = ("sync_pending", "synced", "corrected", "sync_failed", "abandoned")
SyncOp = Literal["edit", "withdraw", "dismiss"]
SYNC_OPS: Final[tuple[SyncOp, ...]] = ("edit", "withdraw", "dismiss")
RemoteKind = Literal["comment", "review"]

SYNCED_ACTIVITY: Final[str] = "annotation.provider_synced"
SYNC_FAILED_ACTIVITY: Final[str] = "annotation.provider_sync_failed"
_MAX_BODY: Final[int] = 20000


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class FeedbackSyncError(RuntimeError):
    """The provider refused or failed the request; its message is shown verbatim."""


class FeedbackSyncConflict(ValueError):
    """The sync request is not allowed in the current state."""


# --------------------------------------------------------------------------- #
# Remote identity
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PublishedRef:
    """Where one comment lives on the provider."""

    annotation_id: str
    remote_ref: str
    kind: RemoteKind
    remote_id: int
    target_ref: str
    """The provider subject: ``github-repo:<durable repository id>#12[@<host>]`` (the host recorded when
    published locally). A legacy by-name ``github:owner/repo#12`` on an old delivery is readable, never synced."""
    revision_id: str
    annotation_version: int

    def to_wire(self) -> dict[str, Any]:
        return {
            "annotation_id": self.annotation_id,
            "remote_ref": self.remote_ref,
            "kind": self.kind,
            "remote_id": self.remote_id,
            "target_ref": self.target_ref,
            "revision_id": self.revision_id,
            "annotation_version": self.annotation_version,
        }


def parse_remote_ref(remote_ref: str) -> tuple[RemoteKind, int] | None:
    prefix, _sep, raw = remote_ref.partition(":")
    if not raw.isdigit():
        return None
    if prefix == "github-comment":
        return "comment", int(raw)
    if prefix == "github-review":
        return "review", int(raw)
    return None


def published_ref_from_deliveries(annotation_id: str, deliveries: Sequence[DeliveryRecord]) -> PublishedRef | None:
    """The comment's current provider identity: its latest sent GitHub delivery.

    An inline comment identity wins over the review that carried it.
    """

    sent = [
        item
        for item in deliveries
        if item.target_type == "github_review" and item.state == "sent" and parse_remote_ref(item.remote_ref)
    ]
    if not sent:
        return None
    ordered = sorted(sent, key=lambda item: (item.annotation_version, item.updated_at or item.created_at))
    comments = [item for item in ordered if item.remote_ref.startswith("github-comment:")]
    chosen = comments[-1] if comments else ordered[-1]
    parsed = parse_remote_ref(chosen.remote_ref)
    assert parsed is not None
    return PublishedRef(
        annotation_id=annotation_id,
        remote_ref=chosen.remote_ref,
        kind=parsed[0],
        remote_id=parsed[1],
        target_ref=chosen.target_ref,
        revision_id=chosen.revision_id,
        annotation_version=max(item.annotation_version for item in sent),
    )


def published_ref(store: Any, annotation: Annotation) -> PublishedRef | None:
    return published_ref_from_deliveries(annotation.id, store.list_deliveries(annotation.id))


# --------------------------------------------------------------------------- #
# Provider seam
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """The exact provider request a sync makes (recorded on the sync row)."""

    method: str
    path: str
    body: Mapping[str, Any] = field(default_factory=dict)
    host: str = ""
    """The GitHub host a durable ``repositories/<id>/...`` request belongs to (``""`` when unrecorded)."""

    def to_wire(self) -> dict[str, Any]:
        wire = {"method": self.method, "path": self.path, "body": dict(self.body)}
        return {**wire, "host": self.host} if self.host else wire


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    remote_ref: str = ""
    url: str = ""


class FeedbackSyncProvider(Protocol):
    """The provider adapter. Each call performs exactly one provider request or raises."""

    provider: str

    def edit_comment(self, target_ref: str, comment_id: int, body: str) -> ProviderResponse: ...

    def delete_comment(self, target_ref: str, comment_id: int) -> ProviderResponse: ...

    def post_correction(self, target_ref: str, body: str) -> ProviderResponse: ...

    def dismiss_review(self, target_ref: str, review_id: int, message: str) -> ProviderResponse: ...


#: A pull request named by its repository's durable GitHub id (survives renames and transfers).
DURABLE_GITHUB_TARGET_PREFIX: Final[str] = "github-repo:"
_HOST: Final[re.Pattern[str]] = re.compile(
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?"
)


def durable_github_target(provider_repo_id: str, number: str, host: str = "") -> str:
    """``github-repo:<repository id>#<number>[@<host>]``, or ``""`` when a part is missing or malformed.

    A repository id is unique only on its GitHub host: a target the local
    ``gh`` will reach records the host that served the pull request. The hosted
    server's targets carry none; its provider installation names the host.
    """

    repo, pull, where = provider_repo_id.strip(), number.strip(), host.strip().lower()
    if not repo or not pull.isdigit() or (where and not _HOST.fullmatch(where)):
        return ""
    return f"{DURABLE_GITHUB_TARGET_PREFIX}{repo}#{pull}" + (f"@{where}" if where else "")


def _split_durable(target_ref: str) -> tuple[str, str, str] | None:
    if not target_ref.startswith(DURABLE_GITHUB_TARGET_PREFIX):
        return None
    repo, _sep, tail = target_ref[len(DURABLE_GITHUB_TARGET_PREFIX) :].rpartition("#")
    number, _at, host = tail.partition("@")
    if not repo or not number.isdigit():
        return None
    return repo, number, host if _HOST.fullmatch(host) else ""


def parse_durable_github_target(target_ref: str) -> tuple[str, str] | None:
    """``(repository id, number)`` of a durable target, else ``None``."""

    parts = _split_durable(target_ref)
    return None if parts is None else parts[:2]


def durable_github_host(target_ref: str) -> str:
    """The GitHub host a durable target recorded; ``""`` when none (or a malformed one) was."""

    parts = _split_durable(target_ref)
    return "" if parts is None else parts[2]


def durable_repository_path(provider_repo_id: str) -> str:
    """The REST prefix a durable target's requests are recorded under."""

    return f"repositories/{urllib.parse.quote(provider_repo_id, safe='')}"


def _github_base(target_ref: str) -> tuple[str, str, str]:
    """``(repository path, pull number, host)`` of a durable target; any other target is refused.

    A by-name target (``github:owner/repo#12``, the form older publishes wrote)
    is never sent: a repository renamed or transferred since would make the name
    reach a different repository. Nothing writes that form any more (publish and
    the hosted adapter record the durable id), so an old delivery that still has
    it is read for display and fails visibly when a sync is attempted.
    """

    durable = _split_durable(target_ref)
    if durable is None:
        raise FeedbackSyncError(
            f"{target_ref or 'this comment'} names its pull request by repository name, not by its durable GitHub id, "
            "so it cannot be changed from here without risking another repository; nothing was sent. "
            "Edit or delete it on GitHub, or discard this request."
        )
    return durable_repository_path(durable[0]), durable[1], durable[2]


def _request_or_conflict(op: str, ref: PublishedRef, *, body: str = "", message: str = "") -> ProviderRequest:
    """:func:`github_requests`, but a target that cannot be addressed durably is a refusal (nothing recorded, nothing sent)."""

    try:
        return github_requests(op, ref, body=body, message=message)
    except FeedbackSyncError as exc:
        raise FeedbackSyncConflict(str(exc)) from exc


def github_requests(op: str, ref: PublishedRef, *, body: str = "", message: str = "") -> ProviderRequest:
    """The GitHub REST request one sync step makes (shared by the local and hosted adapters).

    A durable target is recorded as ``repositories/<id>/...`` with the host it
    was published on; the local adapter sends it there by id, and the hosted
    adapter resolves the id to the repository's current name at call time.
    """

    repo, number, host = _github_base(ref.target_ref)
    if op == "edit_comment":
        return ProviderRequest("PATCH", f"{repo}/pulls/comments/{ref.remote_id}", {"body": body}, host)
    if op == "delete_comment":
        return ProviderRequest("DELETE", f"{repo}/pulls/comments/{ref.remote_id}", {}, host)
    if op == "post_correction":
        return ProviderRequest("POST", f"{repo}/issues/{number}/comments", {"body": body}, host)
    if op == "dismiss_review":
        return ProviderRequest(
            "PUT",
            f"{repo}/pulls/{number}/reviews/{ref.remote_id}/dismissals",
            {"message": message, "event": "DISMISS"},
            host,
        )
    raise ValueError(f"unknown provider sync step {op!r}")


class GitHubRestFeedbackSync:
    """GitHub sync over one request callable; the local and hosted adapters differ only in transport.

    ``call`` performs exactly one :class:`ProviderRequest` (built by
    :func:`github_requests`) and returns the decoded JSON, raising
    :class:`FeedbackSyncError` on any provider failure.
    """

    provider = "github"

    def __init__(self, call: Callable[[ProviderRequest], Any]) -> None:
        self._call = call

    def _ref(self, target_ref: str, remote_id: int) -> PublishedRef:
        return PublishedRef("", "", "comment", remote_id, target_ref, "", 0)

    def edit_comment(self, target_ref: str, comment_id: int, body: str) -> ProviderResponse:
        payload = self._call(github_requests("edit_comment", self._ref(target_ref, comment_id), body=body))
        return ProviderResponse(f"github-comment:{comment_id}", str(_get(payload, "html_url")))

    def delete_comment(self, target_ref: str, comment_id: int) -> ProviderResponse:
        self._call(github_requests("delete_comment", self._ref(target_ref, comment_id)))
        return ProviderResponse(f"github-comment:{comment_id}")

    def post_correction(self, target_ref: str, body: str) -> ProviderResponse:
        payload = self._call(github_requests("post_correction", self._ref(target_ref, 0), body=body))
        identifier = _get(payload, "id")
        if not isinstance(identifier, int):
            raise FeedbackSyncError("GitHub did not return the correction comment id")
        return ProviderResponse(f"github-issue-comment:{identifier}", str(_get(payload, "html_url")))

    def dismiss_review(self, target_ref: str, review_id: int, message: str) -> ProviderResponse:
        payload = self._call(github_requests("dismiss_review", self._ref(target_ref, review_id), message=message))
        return ProviderResponse(f"github-review:{review_id}", str(_get(payload, "html_url")))


class GhFeedbackSyncProvider(GitHubRestFeedbackSync):
    """Local adapter: the reviewer's own ``gh`` login, the same one publish uses."""

    def __init__(self, runner: Callable[[Sequence[str], str | None], str] | None = None) -> None:
        if runner is None:
            from lemoncrow.pro.capabilities.review.sources.github import run_gh

            def _default(args: Sequence[str], stdin: str | None) -> str:
                return run_gh(args, stdin)

            runner = _default
        self._runner = runner
        super().__init__(self._gh)

    def _run(self, args: list[str], stdin: str | None) -> Any:
        from lemoncrow.pro.capabilities.review.sources.github import GitHubCliError

        try:
            raw = self._runner(args, stdin)
        except GitHubCliError as exc:
            raise FeedbackSyncError(str(exc)) from exc
        try:
            return json.loads(raw) if raw.strip() else {}
        except ValueError:
            return {}

    def _gh(self, request: ProviderRequest) -> Any:
        """One ``gh api`` call. A durable ``repositories/<id>/...`` request goes, by id, to its recorded host.

        A repository id is unique only on one GitHub host, so the request is
        sent to the host that served the pull request (never ``gh``'s default)
        and addressed by the id itself: a rename or transfer is followed, and a
        repository that later took the old name is never touched. A durable
        target without a recorded host sends nothing, and a by-name target never
        reaches this adapter (:func:`github_requests` refuses it).
        """

        args = ["api"]
        if request.path.startswith(_DURABLE_PATH):
            if not _HOST.fullmatch(request.host):
                raise FeedbackSyncError(
                    "this comment's pull request was recorded without its GitHub host; nothing was sent"
                )
            args.extend(["--hostname", request.host])
        args.extend(["--method", request.method, request.path])
        stdin: str | None = None
        if request.body:
            args.extend(["--input", "-"])
            stdin = json.dumps(dict(request.body), ensure_ascii=False)
        return self._run(args, stdin)


_DURABLE_PATH: Final[str] = "repositories/"


def _get(payload: Any, key: str) -> Any:
    return payload.get(key, "") if isinstance(payload, Mapping) else ""


# --------------------------------------------------------------------------- #
# Durable record
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SyncEvent:
    at: str
    state: SyncState
    actor_id: str
    detail: str = ""

    def to_wire(self) -> dict[str, str]:
        return {"at": self.at, "state": self.state, "actor_id": self.actor_id, "detail": self.detail}


@dataclass(frozen=True)
class FeedbackSyncRecord:
    """The latest provider sync of one published comment (or review verdict)."""

    id: str
    review_id: str
    revision_id: str
    subject_id: str
    """The annotation id, or ``review:<remote ref>`` for a verdict dismissal."""
    remote_ref: str
    remote_kind: RemoteKind
    target_ref: str
    provider: str
    op: SyncOp
    state: SyncState
    actor_id: str
    requested_body: str = ""
    local_body: str = ""
    """The local text when the request was made: what stays if the provider fails."""
    message: str = ""
    request: Mapping[str, Any] = field(default_factory=dict)
    correction_ref: str = ""
    error: str = ""
    attempts: int = 1
    events: tuple[SyncEvent, ...] = ()
    created_at: str = ""
    updated_at: str = ""
    version: int = 1

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "review_id": self.review_id,
            "revision_id": self.revision_id,
            "subject_id": self.subject_id,
            "remote_ref": self.remote_ref,
            "remote_kind": self.remote_kind,
            "target_ref": self.target_ref,
            "provider": self.provider,
            "op": self.op,
            "state": self.state,
            "actor_id": self.actor_id,
            "requested_body": self.requested_body,
            "local_body": self.local_body,
            "message": self.message,
            "request": dict(self.request),
            "correction_ref": self.correction_ref,
            "error": self.error,
            "attempts": self.attempts,
            "events": [item.to_wire() for item in self.events],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "version": self.version,
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> FeedbackSyncRecord:
        op = str(raw["op"])
        state = str(raw["state"])
        kind = str(raw.get("remote_kind") or "comment")
        if op not in SYNC_OPS or state not in SYNC_STATES or kind not in {"comment", "review"}:
            raise ValueError("not a feedback sync record")
        request = raw.get("request") or {}
        return cls(
            id=str(raw["id"]),
            review_id=str(raw["review_id"]),
            revision_id=str(raw.get("revision_id") or ""),
            subject_id=str(raw.get("subject_id") or ""),
            remote_ref=str(raw.get("remote_ref") or ""),
            remote_kind=cast(RemoteKind, kind),
            target_ref=str(raw.get("target_ref") or ""),
            provider=str(raw.get("provider") or ""),
            op=op,
            state=state,
            actor_id=str(raw.get("actor_id") or ""),
            requested_body=str(raw.get("requested_body") or ""),
            local_body=str(raw.get("local_body") or ""),
            message=str(raw.get("message") or ""),
            request=dict(request) if isinstance(request, Mapping) else {},
            correction_ref=str(raw.get("correction_ref") or ""),
            error=str(raw.get("error") or ""),
            attempts=int(raw.get("attempts") or 1),
            events=tuple(
                SyncEvent(
                    at=str(item.get("at") or ""),
                    state=cast(SyncState, str(item.get("state") or "sync_pending")),
                    actor_id=str(item.get("actor_id") or ""),
                    detail=str(item.get("detail") or ""),
                )
                for item in raw.get("events") or ()
                if isinstance(item, Mapping)
            ),
            created_at=str(raw.get("created_at") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            version=int(raw.get("version") or 1),
        )


def sync_record_id(subject_id: str) -> str:
    return "fsy-" + hashlib.sha256(subject_id.encode("utf-8")).hexdigest()[:32]


class FeedbackSyncLedger:
    _CAS_ATTEMPTS: Final[int] = 8

    def __init__(self, store: Any) -> None:
        self._store = store

    def _row(self, record: FeedbackSyncRecord) -> Any:
        from lemoncrow.pro.capabilities.review.store import DerivedResult

        return DerivedResult(
            review_id=record.review_id,
            revision_id=record.revision_id,
            kind=FEEDBACK_SYNC_ROW_KIND,
            scope_key=record.id,
            stamp=f"{_ROW_STAMP}:{record.version}",
            payload=record.to_wire(),
        )

    def _rows(self, revision_ids: Iterable[str]) -> list[tuple[str, FeedbackSyncRecord]]:
        rows = self._store.derived_results(FEEDBACK_SYNC_ROW_KIND, list(revision_ids))
        out: list[tuple[str, FeedbackSyncRecord]] = []
        for (_revision_id, _scope), (stamp, payload) in rows.items():
            if str(stamp).split(":", 1)[0] != _ROW_STAMP:
                continue
            try:
                out.append((stamp, FeedbackSyncRecord.from_wire(payload)))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def list(self, revision_ids: Iterable[str]) -> tuple[FeedbackSyncRecord, ...]:
        return tuple(sorted((item for _stamp, item in self._rows(revision_ids)), key=lambda item: item.created_at))

    def read(self, revision_id: str, record_id: str) -> tuple[str, FeedbackSyncRecord] | None:
        return next(((stamp, item) for stamp, item in self._rows((revision_id,)) if item.id == record_id), None)

    def begin(self, record: FeedbackSyncRecord) -> FeedbackSyncRecord:
        """Claim a new provider request; refused while another one is pending."""

        for _attempt in range(self._CAS_ATTEMPTS):
            created, stamp, payload = self._store.insert_derived_result(self._row(record))
            if created:
                return record
            current = FeedbackSyncRecord.from_wire(payload)
            if current.state == "sync_pending":
                raise FeedbackSyncConflict("a provider sync of this comment is already in progress")
            claimed = replace(
                record,
                attempts=current.attempts + 1,
                events=(*current.events, *record.events),
                created_at=current.created_at,
                version=current.version + 1,
            )
            if self._store.compare_and_set_derived_result(self._row(claimed), expected_stamp=stamp):
                return claimed
        raise FeedbackSyncConflict("this comment's provider sync is changing concurrently; reload and try again")

    def settle(self, record: FeedbackSyncRecord, **fields: Any) -> FeedbackSyncRecord:
        allowed = {"state", "error", "correction_ref", "request"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"cannot change {', '.join(sorted(unknown))} on a sync record")
        found = self.read(record.revision_id, record.id)
        if found is None:
            raise LookupError("the sync record disappeared")
        stamp, current = found
        now = now_iso()
        state = cast(SyncState, fields.get("state", current.state))
        updated = replace(
            current,
            updated_at=now,
            version=current.version + 1,
            events=(*current.events, SyncEvent(now, state, record.actor_id, str(fields.get("error") or ""))),
            **fields,
        )
        if not self._store.compare_and_set_derived_result(self._row(updated), expected_stamp=stamp):
            raise FeedbackSyncConflict("this comment's provider sync changed concurrently; reload and try again")
        return updated


# --------------------------------------------------------------------------- #
# Sync
# --------------------------------------------------------------------------- #


def _location(annotation: Annotation) -> str:
    anchor = annotation.anchor
    path = str(getattr(anchor, "path", "") or "")
    line = int(getattr(anchor, "start_line", 0) or 0)
    return f"`{path}:{line}`" if path and line else (f"`{path}`" if path else "the change")


def correction_body(annotation: Annotation, op: str, body: str) -> str:
    """The auditable follow-up for a comment GitHub cannot edit in place."""

    where = _location(annotation)
    if op == "withdraw":
        return (
            f"**Correction from LemonCrow review:** the earlier review comment on {where} is withdrawn.\n\n"
            f"> {annotation.body.strip()[:500]}"
        )
    return (
        f"**Correction from LemonCrow review:** the earlier review comment on {where} now reads:\n\n"
        f"{body.strip()}\n\n<details><summary>Previously</summary>\n\n{annotation.body.strip()[:2000]}\n</details>"
    )


@dataclass(frozen=True)
class SyncOutcome:
    record: FeedbackSyncRecord
    annotation: Annotation | None
    """The local comment after the sync (unchanged on failure)."""


def _activity(store: Any, record: FeedbackSyncRecord, *, summary: str) -> None:
    from lemoncrow.pro.capabilities.review.session_models import ReviewActivityEvent

    store.record_activity(
        ReviewActivityEvent(
            id="",
            review_id=record.review_id,
            revision_id=record.revision_id,
            kind=(SYNC_FAILED_ACTIVITY if record.state == "sync_failed" else SYNCED_ACTIVITY),  # type: ignore[arg-type]
            actor_id=record.actor_id,
            actor_type="human",
            subject_type="annotation" if not record.subject_id.startswith("review:") else "review",
            subject_id=record.subject_id,
            summary=summary[:500],
            detail_json=json.dumps(
                {
                    "op": record.op,
                    "state": record.state,
                    "remote_ref": record.remote_ref,
                    "correction_ref": record.correction_ref,
                    "request": dict(record.request),
                    "error": record.error,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
    )


def sync_published_comment(
    store: Any,
    annotation: Annotation,
    *,
    op: Literal["edit", "withdraw"],
    body: str,
    provider: FeedbackSyncProvider,
    actor_id: str,
    apply_local: Callable[[Annotation, str, str], Annotation | None],
    current_version: Callable[[str], int],
) -> SyncOutcome:
    """Edit or withdraw a published comment through the provider, then locally.

    ``apply_local(annotation, op, body)`` writes the local copy (the Reader's
    own annotation update) and runs only after the provider confirmed. On a
    provider failure the record is ``sync_failed`` and nothing local changes.
    """

    ref = published_ref(store, annotation)
    if ref is None:
        raise FeedbackSyncConflict("this comment is not published to the provider; edit it locally")
    if op == "edit":
        if not body.strip():
            raise FeedbackSyncConflict("a comment needs something to say")
        if len(body) > _MAX_BODY:
            raise FeedbackSyncConflict(f"a comment is limited to {_MAX_BODY} characters")
    if ref.kind == "comment":
        step = "edit_comment" if op == "edit" else "delete_comment"
        request = _request_or_conflict(step, ref, body=body)
    else:
        step = "post_correction"
        request = _request_or_conflict(step, ref, body=correction_body(annotation, op, body))
    now = now_iso()
    record = FeedbackSyncLedger(store).begin(
        FeedbackSyncRecord(
            id=sync_record_id(annotation.id),
            review_id=annotation.review_id,
            revision_id=ref.revision_id or annotation.revision_id,
            subject_id=annotation.id,
            remote_ref=ref.remote_ref,
            remote_kind=ref.kind,
            target_ref=ref.target_ref,
            provider=provider.provider,
            op=op,
            state="sync_pending",
            actor_id=actor_id,
            requested_body=body if op == "edit" else "",
            local_body=annotation.body,
            request=request.to_wire(),
            events=(SyncEvent(now, "sync_pending", actor_id),),
            created_at=now,
            updated_at=now,
        )
    )
    return _perform(store, annotation, record, ref, provider, apply_local, current_version)


def _perform(
    store: Any,
    annotation: Annotation,
    record: FeedbackSyncRecord,
    ref: PublishedRef,
    provider: FeedbackSyncProvider,
    apply_local: Callable[[Annotation, str, str], Annotation | None],
    current_version: Callable[[str], int],
) -> SyncOutcome:
    ledger = FeedbackSyncLedger(store)
    correction_ref = ""
    try:
        if ref.kind == "comment" and record.op == "edit":
            provider.edit_comment(ref.target_ref, ref.remote_id, record.requested_body)
        elif ref.kind == "comment":
            provider.delete_comment(ref.target_ref, ref.remote_id)
        else:
            response = provider.post_correction(ref.target_ref, str(record.request.get("body", {}).get("body") or ""))
            correction_ref = response.remote_ref
    except Exception as exc:  # provider boundary: the exact failure is the visible state
        failed = ledger.settle(record, state="sync_failed", error=(str(exc) or exc.__class__.__name__)[:1000])
        _activity(
            store,
            failed,
            summary=f"Provider sync FAILED ({record.op}); the local comment is unchanged: {failed.error}",
        )
        return SyncOutcome(failed, store.get_annotation(annotation.id))
    updated = apply_local(annotation, record.op, record.requested_body)
    if record.op == "edit" and updated is not None:
        # Keep the edited version recorded as published on the provider, so it
        # is never offered for a second publish.
        from lemoncrow.pro.capabilities.review.session_models import DeliveryRecord

        store.record_delivery(
            DeliveryRecord(
                id="",
                annotation_id=annotation.id,
                target_type="github_review",
                target_ref=ref.target_ref,
                state="sent",
                remote_ref=ref.remote_ref,
                operation_id=f"{record.id}:{record.attempts}",
                revision_id=ref.revision_id,
                feedback_hash=hashlib.sha256(record.requested_body.encode("utf-8")).hexdigest(),
                annotation_version=current_version(annotation.id),
            )
        )
    settled = ledger.settle(
        record,
        state="corrected" if ref.kind == "review" else "synced",
        error="",
        correction_ref=correction_ref,
    )
    verb = "edited" if record.op == "edit" else "withdrawn"
    how = (
        f"by a follow-up correction ({correction_ref})"
        if ref.kind == "review"
        else f"on {provider.provider} ({ref.remote_ref})"
    )
    _activity(store, settled, summary=f"Published comment {verb} {how}.")
    return SyncOutcome(settled, updated)


def retry_sync(
    store: Any,
    annotation: Annotation,
    *,
    provider: FeedbackSyncProvider,
    actor_id: str,
    apply_local: Callable[[Annotation, str, str], Annotation | None],
    current_version: Callable[[str], int],
) -> SyncOutcome:
    ledger = FeedbackSyncLedger(store)
    ref = published_ref(store, annotation)
    if ref is None:
        raise FeedbackSyncConflict("this comment is not published to the provider")
    found = ledger.read(ref.revision_id or annotation.revision_id, sync_record_id(annotation.id))
    if found is None or found[1].state != "sync_failed":
        raise FeedbackSyncConflict("there is no failed provider sync to retry")
    failed = found[1]
    if failed.op == "dismiss":
        raise FeedbackSyncConflict("retry a dismissal from the review")
    # A failure recorded before durable identity: refuse before anything is recorded or sent.
    try:
        _github_base(ref.target_ref)
    except FeedbackSyncError as exc:
        raise FeedbackSyncConflict(str(exc)) from exc
    now = now_iso()
    record = ledger.begin(
        replace(
            failed,
            state="sync_pending",
            actor_id=actor_id,
            error="",
            local_body=annotation.body,
            events=(SyncEvent(now, "sync_pending", actor_id, "retry"),),
            updated_at=now,
        )
    )
    return _perform(store, annotation, record, ref, provider, apply_local, current_version)


def discard_failed_sync(store: Any, annotation: Annotation, *, actor_id: str) -> FeedbackSyncRecord:
    """Close a failed request: the provider and local copies stay exactly as they are."""

    ledger = FeedbackSyncLedger(store)
    ref = published_ref(store, annotation)
    revision_id = (ref.revision_id if ref is not None else "") or annotation.revision_id
    found = ledger.read(revision_id, sync_record_id(annotation.id))
    if found is None or found[1].state != "sync_failed":
        raise FeedbackSyncConflict("there is no failed provider sync to discard")
    return ledger.settle(replace(found[1], actor_id=actor_id), state="abandoned")


def dismiss_published_review(
    store: Any,
    *,
    review_id: str,
    revision_id: str,
    remote_ref: str,
    target_ref: str,
    message: str,
    provider: FeedbackSyncProvider,
    actor_id: str,
) -> FeedbackSyncRecord:
    """Dismiss a published review verdict through the provider (recorded; visible on failure)."""

    parsed = parse_remote_ref(remote_ref)
    if parsed is None or parsed[0] != "review":
        raise FeedbackSyncConflict("only a published GitHub review can be dismissed")
    if not message.strip():
        raise FeedbackSyncConflict("a dismissal needs a message for the pull request")
    ref = PublishedRef("", remote_ref, "review", parsed[1], target_ref, revision_id, 0)
    request = _request_or_conflict("dismiss_review", ref, message=message.strip()[:1000])
    now = now_iso()
    subject = f"review:{remote_ref}"
    ledger = FeedbackSyncLedger(store)
    record = ledger.begin(
        FeedbackSyncRecord(
            id=sync_record_id(subject),
            review_id=review_id,
            revision_id=revision_id,
            subject_id=subject,
            remote_ref=remote_ref,
            remote_kind="review",
            target_ref=target_ref,
            provider=provider.provider,
            op="dismiss",
            state="sync_pending",
            actor_id=actor_id,
            message=message.strip()[:1000],
            request=request.to_wire(),
            events=(SyncEvent(now, "sync_pending", actor_id),),
            created_at=now,
            updated_at=now,
        )
    )
    try:
        provider.dismiss_review(target_ref, parsed[1], record.message)
    except Exception as exc:  # provider boundary
        failed = ledger.settle(record, state="sync_failed", error=(str(exc) or exc.__class__.__name__)[:1000])
        _activity(store, failed, summary=f"Dismissing the published review FAILED: {failed.error}")
        return failed
    settled = ledger.settle(record, state="synced", error="")
    _activity(store, settled, summary=f"Published review {remote_ref} dismissed on {provider.provider}.")
    return settled


def sync_view(store: Any, review_id: str, annotations: Sequence[Annotation]) -> dict[str, Any]:
    """Per comment: its provider identity and the latest sync state (for the Reader)."""

    revision_ids = [item.id for item in store.list_revisions(review_id)]
    records = {item.subject_id: item for item in FeedbackSyncLedger(store).list(revision_ids)}
    comments: dict[str, Any] = {}
    for annotation in annotations:
        ref = published_ref(store, annotation)
        record = records.get(annotation.id)
        if ref is None and record is None:
            continue
        comments[annotation.id] = {
            "published": None if ref is None else ref.to_wire(),
            "sync": None if record is None else record.to_wire(),
        }
    reviews = [item.to_wire() for key, item in records.items() if key.startswith("review:")]
    return {"comments": comments, "reviews": reviews}


__all__ = [
    "DURABLE_GITHUB_TARGET_PREFIX",
    "FEEDBACK_SYNC_ROW_KIND",
    "SYNCED_ACTIVITY",
    "SYNC_FAILED_ACTIVITY",
    "SYNC_OPS",
    "SYNC_STATES",
    "FeedbackSyncConflict",
    "FeedbackSyncError",
    "FeedbackSyncLedger",
    "FeedbackSyncProvider",
    "FeedbackSyncRecord",
    "GhFeedbackSyncProvider",
    "GitHubRestFeedbackSync",
    "ProviderRequest",
    "ProviderResponse",
    "PublishedRef",
    "SyncOutcome",
    "correction_body",
    "discard_failed_sync",
    "dismiss_published_review",
    "durable_github_host",
    "durable_github_target",
    "durable_repository_path",
    "github_requests",
    "parse_durable_github_target",
    "parse_remote_ref",
    "published_ref",
    "published_ref_from_deliveries",
    "retry_sync",
    "sync_published_comment",
    "sync_view",
]
