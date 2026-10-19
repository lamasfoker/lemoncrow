"""Client half of local<->hosted Review attachment (G5-SYNC): credentials and the two ports.

The local server never talks to hosted. The ``lc review hosted`` commands do:
they read the hosted projection and replay queued actions with the user's
*managed* hosted login, and record what hosted answered through the local
Reader API. The hosted credential is fail-closed: it is only ever sent to an
origin that is a configured hosted LemonCrow product, and a local machine
token is never sent anywhere but the local server.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from urllib.parse import quote, urlsplit

from lemoncrow_client.config import ClientConfig, load_config
from lemoncrow_client.credentials import managed_credentials_path, read_managed_credentials
from lemoncrow_client.errors import AgentAction, ClientError, ErrorCode
from lemoncrow_client.session import RemoteSession

from lemoncrow.pro.capabilities.review.errors import (
    HostedAttachmentConflict,
    HostedReplayRefused,
    HostedUnavailable,
)
from lemoncrow.pro.capabilities.review.hosted_attachment import ReconnectReport, reconnect

_HOSTED_PRODUCTS = ("review", "core", "runtime")
_LOCAL_TOKEN_PREFIX = "lc_local_"
#: Refusals that mean "hosted is not reachable right now": pending actions stay pending.
_UNAVAILABLE = frozenset(
    {
        ErrorCode.SERVER_UNREACHABLE,
        ErrorCode.SERVER_SESSION_UNAVAILABLE,
        ErrorCode.SHUTTING_DOWN,
        ErrorCode.DEADLINE_EXCEEDED,
        ErrorCode.CONCURRENCY_LIMIT,
        ErrorCode.DISPATCHER_UNAVAILABLE,
        ErrorCode.INTERNAL,
    }
)


def _origin(url: str) -> str:
    parts = urlsplit(url.strip())
    return f"{parts.scheme}://{parts.netloc.lower()}" if parts.scheme and parts.netloc else ""


def hosted_attachment_config(
    local: ClientConfig, origin: str = "", *, env: dict[str, str] | None = None
) -> ClientConfig:
    """The hosted endpoint and managed login an attachment uses; refuses anything ambiguous.

    *origin* must equal the URL of a LemonCrow product configured in hosted
    mode; without it exactly one such product must exist. Only the managed
    ``lc auth login`` credential is used -- never ``LEMONCROW_TOKEN`` or the
    local machine token -- because the managed file does not record its
    origin and an operator token may belong to a different server.
    """

    candidates: dict[str, ClientConfig] = {}
    for product in _HOSTED_PRODUCTS:
        try:
            config = load_config(env, cwd=local.repo_root, product=product)
        except ClientError:
            continue
        if config.hosted and _origin(config.url):
            candidates.setdefault(_origin(config.url), config)
    if origin:
        chosen = candidates.get(_origin(origin))
        if chosen is None:
            raise ClientError(
                ErrorCode.NOT_CONFIGURED,
                f"{_origin(origin) or origin!r} is not a configured hosted LemonCrow server; "
                "target it with `lemoncrow-client target` first",
                action=AgentAction.FIX_REQUEST,
            )
    elif len(candidates) == 1:
        chosen = next(iter(candidates.values()))
    else:
        raise ClientError(
            ErrorCode.NOT_CONFIGURED,
            (
                "no hosted LemonCrow server is configured"
                if not candidates
                else "several hosted LemonCrow servers are configured; pass --origin"
            ),
            action=AgentAction.FIX_REQUEST,
        )
    access, refresh = read_managed_credentials(chosen.account_state_dir)
    path = managed_credentials_path(chosen.account_state_dir)
    if not access and not refresh:
        access, refresh = read_managed_credentials(chosen.state_dir)
        path = managed_credentials_path(chosen.state_dir)
    if not access or access.startswith(_LOCAL_TOKEN_PREFIX):
        raise ClientError(
            ErrorCode.UNAUTHENTICATED,
            "no hosted login for this server; run `lc auth login` first",
            action=AgentAction.REAUTHENTICATE,
        )
    return replace(
        chosen,
        token=access,
        token_source=str(path),
        refresh_token=refresh,
        refresh_token_source=str(path) if refresh else "",
    )


def parse_hosted_reference(reference: str) -> str:
    """``r/<id>``, a hosted ``/r/<id>`` URL, or a bare id -> the hosted Review id."""

    text = reference.strip()
    path = urlsplit(text).path if "://" in text else text
    parts = path.strip("/").split("/")
    if "/" in path:
        token = parts[-1] if len(parts) >= 2 and parts[-2] == "r" else ""
    else:
        token = path
    if not token.strip(".") or any(not (char.isalnum() or char in "_.:-") for char in token):
        raise ClientError(ErrorCode.PAYLOAD_INVALID, f"not a hosted Review reference: {reference!r}")
    return token


def _hosted_error(exc: ClientError) -> Exception:
    if exc.code in _UNAVAILABLE or exc.retryable:
        return HostedUnavailable(exc.message)
    if exc.code == ErrorCode.PAYLOAD_INVALID:
        return HostedReplayRefused("rejected", exc.message)
    if exc.code == ErrorCode.FORBIDDEN:
        return HostedReplayRefused("forbidden", exc.message)
    if exc.code == ErrorCode.UNAUTHENTICATED:
        return HostedReplayRefused("unauthenticated", exc.message)
    return HostedReplayRefused(exc.server_code or "refused", exc.message)


class RemoteHostedPort:
    """The attached hosted Review, spoken to as the managed hosted login."""

    def __init__(self, config: ClientConfig, repo_id: str, review_id: str) -> None:
        self._config = config
        self._repo_id = repo_id
        self._review_id = review_id
        self._remote = RemoteSession(config)

    def projection(self) -> Mapping[str, Any]:
        try:
            return dict(
                self._remote.review_attachment_projection(
                    self._repo_id, self._review_id, timeout_s=self._config.request_timeout_s
                )
            )
        except ClientError as exc:
            raise _hosted_error(exc) from exc

    def replay(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            return dict(
                self._remote.review_attachment_replay(
                    self._repo_id, self._review_id, request, timeout_s=self._config.request_timeout_s
                )
            )
        except ClientError as exc:
            raise _hosted_error(exc) from exc

    def find_repo_id(self) -> str:
        """The hosted repository of this Review, from the hosted inbox."""

        try:
            rows = self._remote.review_inbox(timeout_s=self._config.request_timeout_s).get("reviews")
        except ClientError as exc:
            raise _hosted_error(exc) from exc
        for row in rows if isinstance(rows, list) else ():
            if isinstance(row, Mapping) and row.get("id") == self._review_id and row.get("repo_id"):
                return str(row["repo_id"])
        return ""

    def close(self) -> None:
        self._remote.close()


class ReaderLocalPort:
    """The attached local Review, through the local server's Reader API."""

    def __init__(self, config: ClientConfig, review_id: str) -> None:
        self._config = config
        self._base = f"/api/reviews/{quote(review_id, safe='')}/hosted"
        self._remote = RemoteSession(config)
        self._attachment: dict[str, Any] = {}

    def _request(self, method: str, suffix: str = "", body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return dict(
            self._remote.review_reader_request(
                method,
                f"{self._base}{suffix}",
                body=None if body is None else dict(body),
                timeout_s=self._config.tool_timeout_s,
            )
        )

    def status(self) -> Mapping[str, Any]:
        payload = self._request("GET")
        self._attachment = dict(payload.get("attachment") or {})
        return payload

    def attach(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return self._request("PUT", body=body)

    def detach(self, *, discard_unresolved: bool) -> dict[str, Any]:
        return self._request("DELETE", "?discard_unresolved=true" if discard_unresolved else "")

    def queue(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/actions", body=body)

    def resolve(self, action_id: str, op: str) -> dict[str, Any]:
        return self._request("POST", f"/actions/{quote(action_id, safe='')}", body={"op": op})

    def record_projection(self, projection: Mapping[str, Any]) -> Mapping[str, Any]:
        attachment = self._attachment or dict(self.status().get("attachment") or {})
        return self.attach(
            {
                "origin": attachment.get("origin"),
                "hosted_repo_id": attachment.get("hosted_repo_id"),
                "hosted_review_id": attachment.get("hosted_review_id"),
                "principal_id": attachment.get("principal_id"),
                "projection": dict(projection),
            }
        )

    def record_result(
        self,
        action_id: str,
        *,
        expected_state: str,
        state: str,
        reason: str,
        result: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        try:
            return self._request(
                "POST",
                f"/actions/{quote(action_id, safe='')}",
                body={
                    "op": "result",
                    "expected_state": expected_state,
                    "state": state,
                    "reason": reason,
                    "result": dict(result),
                },
            )
        except ClientError as exc:
            if exc.code == ErrorCode.REVIEW_STATE_CONFLICT:
                raise HostedAttachmentConflict("action_state_changed", exc.message) from exc
            raise

    def close(self) -> None:
        self._remote.close()


def attach_review(
    local: ClientConfig, hosted: ClientConfig, local_review_id: str, hosted_reference: str, *, repo_id: str = ""
) -> dict[str, Any]:
    """Explicitly attach a local Review to a hosted one as the managed hosted login."""

    hosted_id = parse_hosted_reference(hosted_reference)
    hosted_repo = repo_id
    if not hosted_repo:
        finder = RemoteHostedPort(hosted, "_", hosted_id)
        try:
            hosted_repo = finder.find_repo_id()
        except (HostedUnavailable, HostedReplayRefused) as exc:
            raise ClientError(ErrorCode.SERVER_UNREACHABLE, f"hosted inbox unavailable: {exc}") from exc
        finally:
            finder.close()
    if not hosted_repo:
        raise ClientError(
            ErrorCode.REVIEW_UNKNOWN,
            f"hosted r/{hosted_id} is not in your hosted inbox; pass --repo-id",
            action=AgentAction.FIX_REQUEST,
        )
    port = RemoteHostedPort(hosted, hosted_repo, hosted_id)
    local_port = ReaderLocalPort(local, local_review_id)
    try:
        try:
            projection = port.projection()
        except (HostedUnavailable, HostedReplayRefused) as exc:
            raise ClientError(ErrorCode.SERVER_UNREACHABLE, f"hosted refused the attachment: {exc}") from exc
        return local_port.attach(
            {
                "origin": _origin(hosted.url),
                "hosted_repo_id": hosted_repo,
                "hosted_review_id": hosted_id,
                "principal_id": projection.get("principal_id"),
                "projection": dict(projection),
            }
        )
    finally:
        port.close()
        local_port.close()


def sync_review(local: ClientConfig, hosted: ClientConfig, local_review_id: str) -> ReconnectReport:
    """Reconnect: replay every pending action, record each answer, refresh the projection."""

    local_port = ReaderLocalPort(local, local_review_id)
    try:
        attachment = dict(local_port.status().get("attachment") or {})
        if not attachment:
            return ReconnectReport(identity="not_attached")
        if _origin(str(attachment.get("origin") or "")) != _origin(hosted.url):
            # Fail closed: the hosted login must belong to the origin this Review is attached to.
            return ReconnectReport(identity="origin_mismatch")
        hosted_port = RemoteHostedPort(hosted, str(attachment["hosted_repo_id"]), str(attachment["hosted_review_id"]))
        try:
            return reconnect(local_port, hosted_port)
        finally:
            hosted_port.close()
    finally:
        local_port.close()


__all__ = [
    "ReaderLocalPort",
    "RemoteHostedPort",
    "attach_review",
    "hosted_attachment_config",
    "parse_hosted_reference",
    "sync_review",
]
