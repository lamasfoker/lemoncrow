"""The base a mutable branch Review diffs against, from the git command line.

This is a standard-library port of the engine's pygit2 rule (``gitdiff.
review_branch_base``). The thin client ships without pygit2, so it asks
``git`` the same ancestry questions:

- Start from the Review's recorded base. If ``HEAD`` no longer descends from
  it (history rewritten below it), fall back to ``merge-base(HEAD, base)``.
- For each integration tip, move the base to ``merge-base(HEAD, tip)`` when
  that point descends from the current base. Integration tips are the
  remote-tracking refs of ``origin`` and ``upstream``: the remote ``HEAD``'s
  symbolic target, then ``main``, then ``master``. A local ``main`` never
  counts: it may carry the Review's own unpushed commits.

Work the branch absorbs from its integration branch (pull, merge, rebase) is
someone else's change, so it leaves the Review; the author's own commits stay.
``tests/gateway/test_review_base_client_parity.py`` checks this module against
the pygit2 rule over generated histories. A recorded base this clone lacks
yields ``""`` under both (use ``HEAD``, plan §5).

The recorded base comes from one read both servers serve, keyed by the
capture's subject: :data:`BRANCH_REVIEW_PATH`. :func:`parse_branch_review`
reads its answer and :func:`branch_review_failed` is the refusal a capture
raises when the read fails, for the thin client and the engine alike.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, NamedTuple

from .errors import AgentAction, ClientError, ErrorCode

__all__ = [
    "BRANCH_REVIEW_PATH",
    "EMPTY_TREE_SHA",
    "BranchReview",
    "branch_review_failed",
    "capture_review_base",
    "parse_branch_review",
    "review_branch_base",
]

#: Git's empty tree. A Review recorded against it started before any commit.
EMPTY_TREE_SHA: Final[str] = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

#: The subject-keyed lookup of a branch's open working-tree or staged Review:
#: ``?range_mode=&branch=`` plus the repository, answered ``{"review": null |
#: {id, repo_id, range_mode, status, review_base_sha, review_base_policy}}``.
#: Every server that honors the ``workspace_branch`` capture option serves it,
#: except a local server from before the lookup (it answers 404).
BRANCH_REVIEW_PATH: Final[str] = "/api/branch-review"

#: Server guidance that a retry cannot satisfy on its own; kept on a failed lookup.
_KEPT_ACTIONS: Final[frozenset[AgentAction]] = frozenset(
    {
        AgentAction.REAUTHENTICATE,
        AgentAction.REQUEST_ACCESS,
        AgentAction.UPGRADE_CLIENT,
        AgentAction.UPGRADE_SERVER,
    }
)


class BranchReview(NamedTuple):
    """A branch's open Review as the lookup names it; blank fields when it has none."""

    review_id: str
    base_sha: str
    policy: str


def _malformed(message: str) -> ClientError:
    return ClientError(ErrorCode.INTERNAL, f"the branch Review lookup answered {message}")


def parse_branch_review(payload: Mapping[str, Any], *, range_mode: str) -> BranchReview:
    """The open Review in a lookup answer, or a blank one for ``{"review": null}``.

    An answer that is not the documented shape, or names a Review that is not
    open or not of *range_mode*, is refused: the capture must not guess a base.
    """

    if "review" not in payload:
        raise _malformed("without a review field")
    raw = payload["review"]
    if raw is None:
        return BranchReview("", "", "")
    if not isinstance(raw, Mapping):
        raise _malformed("a review that is not an object")
    review_id, base, policy = raw.get("id"), raw.get("review_base_sha"), raw.get("review_base_policy")
    if not isinstance(review_id, str) or not review_id.strip():
        raise _malformed("a review without an id")
    if raw.get("status") != "open" or raw.get("range_mode") != range_mode:
        raise _malformed(f"a review that is not an open {range_mode} Review")
    if not isinstance(base, str) or not isinstance(policy, str):
        raise _malformed("a review whose base or policy is not a string")
    return BranchReview(review_id.strip(), base.strip(), policy)


def branch_review_failed(branch: str, error: ClientError) -> ClientError:
    """The refusal a capture raises when the lookup of *branch*'s Review fails.

    The capture fails rather than fall back to ``HEAD``: routed to the
    branch's Review with ``HEAD`` as its base, it would record a Snapshot that
    silently drops the Review's commits. Nothing was captured, so a retry is
    safe; the server's own guidance is kept when a retry alone cannot help.
    """

    return ClientError(
        error.code,
        (
            f"could not read the base of branch {branch!r}'s Review from the server ({error.message}); "
            "nothing was captured. Retry the capture"
        ),
        details={**error.details, "lookup": BRANCH_REVIEW_PATH, "branch": branch},
        retryable=True,
        action=error.action if error.action in _KEPT_ACTIONS else AgentAction.RETRY_LATER,
        server_code=error.server_code,
    )


_INTEGRATION_REMOTES: Final[tuple[str, ...]] = ("origin", "upstream")
_INTEGRATION_BRANCHES: Final[tuple[str, ...]] = ("main", "master")
_GIT_TIMEOUT_S: Final[float] = 30.0
_REF_FORMAT: Final[str] = "%(refname)%00%(symref)%00%(objectname)%00%(objecttype)"


class _GitFailed(Exception):
    """git could not answer an ancestry question; the caller keeps the recorded base."""


def _git(repo: Path, *arguments: str) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repo,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            capture_output=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _GitFailed(str(exc)) from exc
    return completed.returncode, completed.stdout.decode("utf-8", errors="replace").strip()


def _commit(repo: Path, spec: str) -> str:
    """Full id of the commit *spec* peels to, or ``""`` when this clone has none."""

    if not spec or spec.startswith("-"):
        return ""
    code, out = _git(repo, "rev-parse", "--verify", "--quiet", f"{spec}^{{commit}}")
    return out if code == 0 else ""


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    """True when *descendant* is *ancestor* or descends from it."""

    code, _ = _git(repo, "merge-base", "--is-ancestor", ancestor, descendant)
    if code not in (0, 1):
        raise _GitFailed(f"merge-base --is-ancestor exited {code}")
    return code == 0


def _merge_base(repo: Path, left: str, right: str) -> str:
    """One best common ancestor, or ``""`` for unrelated histories."""

    code, out = _git(repo, "merge-base", left, right)
    if code == 1 and not out:
        return ""
    if code != 0 or not out:
        raise _GitFailed(f"merge-base exited {code}")
    return out.split()[0]


def _refs(repo: Path, names: list[str]) -> dict[str, tuple[str, str, str]]:
    """``{refname: (symbolic target, object id, object type)}`` for the exact *names* that exist."""

    if not names:
        return {}
    code, out = _git(repo, "for-each-ref", f"--format={_REF_FORMAT}", *names)
    if code != 0:
        raise _GitFailed(f"for-each-ref exited {code}")
    wanted = set(names)
    found: dict[str, tuple[str, str, str]] = {}
    for line in out.splitlines():
        fields = line.split("\0")
        # A pattern also matches refs below it (``main/x``); only exact names count.
        if len(fields) == 4 and fields[0] in wanted:
            found[fields[0]] = (fields[1], fields[2], fields[3])
    return found


def _integration_tips(repo: Path) -> tuple[str, ...]:
    """Commit ids of the remote branches this repository integrates into, in rule order."""

    candidates = [
        f"refs/remotes/{remote}/{branch}"
        for remote in _INTEGRATION_REMOTES
        for branch in ("HEAD", *_INTEGRATION_BRANCHES)
    ]
    refs = _refs(repo, candidates)
    names: list[str] = []
    for remote in _INTEGRATION_REMOTES:
        symbolic = refs.get(f"refs/remotes/{remote}/HEAD")
        # Only a symbolic remote HEAD names a branch; a detached one is ignored.
        if symbolic is not None and symbolic[0]:
            names.append(symbolic[0])
        names.extend(f"refs/remotes/{remote}/{branch}" for branch in _INTEGRATION_BRANCHES)
    ordered = list(dict.fromkeys(names))
    refs.update(_refs(repo, [name for name in ordered if name not in refs]))
    tips: list[str] = []
    for name in ordered:
        entry = refs.get(name)
        if entry is None:
            continue
        _, oid, kind = entry
        tip = oid if kind == "commit" else _commit(repo, oid)
        if tip and tip not in tips:
            tips.append(tip)
    return tuple(tips)


def _advance(repo: Path, frozen: str, anchor: str, head: str, tips: tuple[str, ...]) -> str:
    if anchor and not _is_ancestor(repo, anchor, head):
        rewound = _merge_base(repo, head, anchor)
        if not rewound:
            return frozen
        anchor = rewound
    for tip in tips:
        point = _merge_base(repo, head, tip)
        if not point:
            continue
        if not anchor or _is_ancestor(repo, anchor, point):
            anchor = point
    return anchor or frozen


def review_branch_base(repo: Path, frozen_base: str, *, head: str = "") -> str:
    """The base a mutable branch Review diffs *head* (default ``HEAD``) against.

    ``""`` means "no branch base: use ``HEAD``". Answers:

    - blank *frozen_base*: ``""``;
    - *frozen_base* not a commit in this clone (a shallow or partial clone, or
      a spec that is no commit): ``""``. The engine's pygit2 rule returns
      *frozen_base* here and then fails to read it; plan §5 says use ``HEAD``;
    - *head* unresolvable or unborn, or ``HEAD`` unrelated to the base:
      *frozen_base* stripped, unchanged;
    - ``EMPTY_TREE_SHA``: the first integration point, else ``EMPTY_TREE_SHA``;
    - otherwise the full id of the advanced base commit.

    Apart from the missing base this matches ``gitdiff.review_branch_base``.
    A detached ``HEAD`` is advanced like any other (``capture_review_base``
    refuses it). If git fails after the base resolved (the pygit2 rule would
    raise), the stripped *frozen_base* is returned.
    """

    frozen = frozen_base.strip()
    if not frozen:
        return ""
    try:
        anchor = "" if frozen == EMPTY_TREE_SHA else _commit(repo, frozen)
    except _GitFailed:
        return ""
    if frozen != EMPTY_TREE_SHA and not anchor:
        return ""
    try:
        head_id = _commit(repo, head or "HEAD")
        if not head_id:
            return frozen
        return _advance(repo, frozen, anchor, head_id, _integration_tips(repo))
    except _GitFailed:
        return frozen


def capture_review_base(repo: Path, recorded_base: str) -> str:
    """Base for a working-tree or staged capture under a branch Review.

    Returns ``""`` when the capture must use ``HEAD`` as today: no recorded
    base, a detached ``HEAD`` (a rebase in progress included), an unborn or
    unresolvable ``HEAD``, a recorded base this clone does not have, or a
    git failure. Otherwise it returns the advanced base as a full commit id,
    or ``EMPTY_TREE_SHA`` for a Review recorded before the first commit that
    has no integration point yet; the caller materializes that as the empty
    tree. The result is always a commit or the empty tree, never a spec.
    """

    frozen = recorded_base.strip()
    if not frozen:
        return ""
    try:
        code, _ = _git(repo, "symbolic-ref", "--quiet", "HEAD")
        if code != 0 or not _commit(repo, "HEAD"):
            return ""
        base = review_branch_base(repo, frozen)
        # The rule hands back the recorded base as given when HEAD is unrelated
        # to it; an abbreviated one becomes a full id here.
        return base if base == EMPTY_TREE_SHA else _commit(repo, base)
    except _GitFailed:
        return ""
