"""The thin client's base rule agrees with the engine's over generated histories.

``lemoncrow_client.review_base`` ports ``gitdiff.review_branch_base`` (pygit2)
to the git command line. Each case builds real repositories with pygit2 from a
seed, replays a Review's life on them (own commits, pulls, merges and rebases
from integration, force moves, pushes, criss-cross merges, remote ``HEAD``
changes, decoy local branches, a detached ``HEAD``) and asks both rules for the
base after every step and for many recorded bases at the end.

A divergence is classified against the reference answer:

- missing base: the recorded base is not a commit in the clone. Both the
  thin client and the pygit2 rule answer ``""`` (use ``HEAD``, plan §5).
  A separate assertion pins that fallback for both implementations;
- failing closed: the client's base is an ancestor of the reference base, so
  the client shows more to judge;
- failing open: anything else, including an answer that is not a commit. The
  client would hide work the engine shows.

The target is zero divergences, including missing-base histories.
"""

from __future__ import annotations

import random
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pygit2
import pytest
from lemoncrow_client import review_base

from lemoncrow.pro.capabilities.review import gitdiff

EMPTY = gitdiff.EMPTY_TREE_SHA
CASES = 40
HISTORIES_PER_CASE = 5
STEPS = 12

# Integration lines and the remote-tracking ref each one is published under.
LINES: dict[str, str] = {
    "main": "refs/remotes/origin/main",
    "master": "refs/remotes/origin/master",
    "develop": "refs/remotes/origin/develop",
    "up_main": "refs/remotes/upstream/main",
    "up_master": "refs/remotes/upstream/master",
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)


@dataclass
class History:
    root: Path
    rng: random.Random
    skew: bool
    repo: object = field(init=False)
    clock: int = 1_700_000_000
    serial: int = 0
    commits: list[str] = field(default_factory=list)
    lines: dict[str, str | None] = field(default_factory=dict)
    feature: str = ""
    detached: bool = False
    ops: list[str] = field(default_factory=list)
    # Remote-tracking refs pinned to a non-commit object (annotated tag, tree).
    raw: dict[str, str] = field(default_factory=dict)
    nested: bool = False

    def __post_init__(self) -> None:
        self.repo = pygit2.init_repository(str(self.root), initial_head="feature")

    # -- object construction -------------------------------------------------

    def commit(self, *parents: str) -> str:
        self.serial += 1
        self.clock += 60
        when = self.clock + (self.rng.randint(-3600, 3600) if self.skew else 0)
        signature = pygit2.Signature("t", "t@example.test", when, 0)
        builder = self.repo.TreeBuilder()
        builder.insert("f", self.repo.create_blob(f"{self.serial}\n".encode()), pygit2.enums.FileMode.BLOB)
        oid = self.repo.create_commit(
            None, signature, signature, f"c{self.serial}", builder.write(), [pygit2.Oid(hex=p) for p in parents]
        )
        sha = str(oid)
        self.commits.append(sha)
        return sha

    def set_ref(self, name: str, target: str | None) -> None:
        refs = self.repo.references
        if target is None:
            if name in refs:
                refs.delete(name)
            return
        if target.startswith("refs/"):
            refs.create(name, target, force=True)
        else:
            refs.create(name, pygit2.Oid(hex=target), force=True)

    def publish(self) -> None:
        for line, ref in LINES.items():
            if ref in self.raw:
                continue
            tip = self.lines.get(line)
            if tip:
                self.set_ref(f"{ref}/wip", None)
            self.set_ref(ref, tip)
            if not tip and self.nested:
                # Matches the ref as a for-each-ref pattern but is not the ref.
                self.set_ref(f"{ref}/wip", self.rng.choice(self.commits))
        for ref, oid in self.raw.items():
            self.set_ref(f"{ref}/wip", None)
            self.repo.references.create(ref, pygit2.Oid(hex=oid), force=True)
        self.set_ref("refs/heads/feature", self.feature)
        if self.detached:
            self.repo.set_head(pygit2.Oid(hex=self.rng.choice([self.feature, *self.commits])))
        else:
            self.repo.set_head("refs/heads/feature")

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        if ancestor == descendant:
            return True
        return bool(self.repo.descendant_of(pygit2.Oid(hex=descendant), pygit2.Oid(hex=ancestor)))

    def own_commits(self, upstream: str) -> list[str]:
        walker = self.repo.walk(
            pygit2.Oid(hex=self.feature), pygit2.enums.SortMode.TOPOLOGICAL | pygit2.enums.SortMode.REVERSE
        )
        walker.hide(pygit2.Oid(hex=upstream))
        return [str(c.id) for c in walker if len(c.parent_ids) == 1]

    # -- operations -----------------------------------------------------------

    def live_line(self) -> str:
        live = [name for name, tip in self.lines.items() if tip]
        return self.rng.choice(live) if live else "main"

    def tip(self, line: str) -> str:
        tip = self.lines.get(line)
        if not tip:
            tip = self.commit(self.rng.choice(self.commits))
            self.lines[line] = tip
        return tip

    def step(self) -> None:
        rng = self.rng
        op = rng.choices(
            [
                "work",
                "advance",
                "pull_merge",
                "pull_ff",
                "rebase",
                "force",
                "push_merge",
                "push_ff",
                "criss_cross",
                "amend",
                "orphan",
                "remote_head",
                "decoys",
                "tag_tip",
                "drop_line",
                "detach",
            ],
            weights=[6, 5, 3, 2, 3, 1, 2, 1, 2, 1, 1, 2, 1, 1, 1, 2],
        )[0]
        self.ops.append(op)
        line = self.live_line()
        if op == "work":
            self.feature = self.commit(self.feature)
        elif op == "advance":
            self.lines[line] = self.commit(self.tip(line))
        elif op == "pull_merge":
            self.feature = self.commit(self.feature, self.tip(line))
        elif op == "pull_ff":
            tip = self.tip(line)
            self.feature = tip if self.is_ancestor(self.feature, tip) else self.commit(self.feature, tip)
        elif op == "rebase":
            tip = self.tip(line)
            replayed = tip
            for _ in self.own_commits(tip):
                replayed = self.commit(replayed)
            self.feature = replayed
        elif op == "force":
            self.feature = rng.choice(self.commits)
        elif op == "push_merge":
            self.lines[line] = self.commit(self.tip(line), self.feature)
        elif op == "push_ff":
            if self.is_ancestor(self.tip(line), self.feature):
                self.lines[line] = self.feature
        elif op == "criss_cross":
            tip, before = self.tip(line), self.feature
            self.feature = self.commit(before, tip)
            self.lines[line] = self.commit(tip, before)
        elif op == "amend":
            parents = [str(p) for p in self.repo[pygit2.Oid(hex=self.feature)].parent_ids]
            self.feature = self.commit(*parents)
        elif op == "orphan":
            self.lines[line] = self.commit()
        elif op == "remote_head":
            for remote in ("origin", "upstream"):
                choice = rng.choice(["main", "master", "develop", "direct", "none", "dangling", "local"])
                name = f"refs/remotes/{remote}/HEAD"
                if choice == "none":
                    self.set_ref(name, None)
                elif choice == "direct":
                    self.set_ref(name, rng.choice(self.commits))
                elif choice == "dangling":
                    self.set_ref(name, f"refs/remotes/{remote}/gone")
                elif choice == "local":
                    # A remote HEAD pointing at a local branch counts in both rules.
                    self.set_ref(name, "refs/heads/main")
                else:
                    self.set_ref(name, f"refs/remotes/{remote}/{choice}")
        elif op == "decoys":
            # Local branches that look like integration branches never count.
            for name in ("refs/heads/main", "refs/heads/master", "refs/heads/origin/main", "refs/heads/upstream/main"):
                self.set_ref(name, rng.choice([self.feature, self.commit(self.feature), rng.choice(self.commits)]))
            self.set_ref("refs/remotes/origin/mainline", self.commit(self.feature))
            self.set_ref("refs/remotes/fork/main", self.commit(self.feature))
            self.nested = True
        elif op == "tag_tip":
            # upstream/master holds an annotated tag (peeled) or a tree (skipped).
            tagger = pygit2.Signature("t", "t@example.test", self.clock, 0)
            target = self.tip(line)
            self.serial += 1
            if rng.random() < 0.7:
                tag = self.repo.create_tag(
                    f"t{self.serial}", pygit2.Oid(hex=target), pygit2.enums.ObjectType.COMMIT, tagger, "tag"
                )
                self.raw["refs/remotes/upstream/master"] = str(tag)
            else:
                tree = self.repo[pygit2.Oid(hex=target)].tree_id
                self.raw["refs/remotes/upstream/master"] = str(tree)
        elif op == "drop_line":
            self.lines[line] = None
        elif op == "detach":
            self.detached = not self.detached
        self.publish()


def _not_in_clone(root: Path, frozen: str) -> bool:
    spec = frozen.strip()
    if not spec or spec == EMPTY:
        return False
    return spec.startswith("-") or _git(root, "rev-parse", "--verify", "-q", f"{spec}^{{commit}}").returncode != 0


def _classify(history: History, frozen: str, reference: str, client: str) -> str:
    if reference == client:
        return "equal"
    if client == "" and _not_in_clone(history.root, frozen):
        return "missing_base"
    known = set(history.commits)
    if client == EMPTY and reference in known:
        return "closed"
    if client in known and reference in known and history.is_ancestor(client, reference):
        return "closed"
    return "open"


def _compare(history: History, frozen: str, *, head: str = "") -> tuple[str, str, str] | None:
    reference = gitdiff.review_branch_base(history.root, frozen, head=head)
    client = review_base.review_branch_base(history.root, frozen, head=head)
    verdict = _classify(history, frozen, reference, client)
    if verdict == "equal":
        return None
    return (verdict, f"frozen={frozen!r} head={head!r} ops={history.ops}", f"reference={reference} client={client}")


def _assert_missing_base_uses_head(history: History, frozen: str) -> None:
    """A base the clone lacks: both implementations say use HEAD."""

    assert review_base.review_branch_base(history.root, frozen) == ""
    assert review_base.capture_review_base(history.root, frozen) == ""
    assert gitdiff.review_branch_base(history.root, frozen) == ""


def _frozen_candidates(history: History, recorded: list[str]) -> list[str]:
    rng = history.rng
    picks = rng.sample(history.commits, min(4, len(history.commits)))
    sample = recorded[:: max(1, len(recorded) // 4)]
    some = rng.choice(history.commits)
    return [
        *dict.fromkeys([*sample, *picks]),
        EMPTY,
        "",
        "   ",
        some[:12],
        f"  {some}\n",
    ]


def _run_history(root: Path, seed: int) -> tuple[list[tuple[str, str, str]], int]:
    rng = random.Random(seed)
    history = History(root, rng, skew=rng.random() < 0.3)
    trunk = history.commit()
    for _ in range(rng.randint(1, 4)):
        trunk = history.commit(trunk)
    history.lines = {"main": trunk}
    if rng.random() < 0.5:
        history.lines["master"] = history.commit(trunk)
    if rng.random() < 0.5:
        history.lines["up_main"] = trunk
    if rng.random() < 0.3:
        history.lines["develop"] = history.commit(trunk)
    history.feature = history.commit(trunk)
    history.publish()
    if rng.random() < 0.7:
        history.set_ref(
            "refs/remotes/origin/HEAD", rng.choice(["refs/remotes/origin/main", "refs/remotes/origin/develop"])
        )
    first_base = trunk
    recorded = [first_base]
    divergences: list[tuple[str, str, str]] = []
    comparisons = 0
    for _ in range(STEPS):
        history.step()
        recorded.append(history.feature)
        comparisons += 1
        if (found := _compare(history, first_base)) is not None:
            divergences.append(found)
    for frozen in _frozen_candidates(history, recorded):
        comparisons += 1
        if (found := _compare(history, frozen)) is not None:
            divergences.append(found)
    head = rng.choice(history.commits)
    comparisons += 1
    if (found := _compare(history, first_base, head=head)) is not None:
        divergences.append(found)
    # Bases this clone does not have: an unknown commit id and a tree.
    tree = str(history.repo[pygit2.Oid(hex=history.feature)].tree_id)
    for missing in (f"{rng.getrandbits(160):040x}", tree):
        comparisons += 1
        if (found := _compare(history, missing)) is not None:
            divergences.append(found)
        if history.detached:
            assert review_base.review_branch_base(history.root, missing) == ""
        else:
            _assert_missing_base_uses_head(history, missing)
    return divergences, comparisons


@pytest.mark.parametrize("case", range(CASES))
def test_client_rule_matches_the_engine_rule_over_generated_histories(tmp_path: Path, case: int) -> None:
    divergences: list[tuple[str, str, str]] = []
    for index in range(HISTORIES_PER_CASE):
        seed = case * HISTORIES_PER_CASE + index
        found, _ = _run_history(tmp_path / f"h{seed}", seed)
        divergences.extend((verdict, f"seed={seed} {where}", answers) for verdict, where, answers in found)
    fail_open = [d for d in divergences if d[0] == "open"]
    assert not fail_open, "client base hides work the engine shows:\n" + "\n".join(map(str, fail_open))
    assert not divergences, "client and engine base rules disagree:\n" + "\n".join(map(str, divergences))


# ---------------------------------------------------------------------------
# scripted histories: every listed shape exists, not just probably
# ---------------------------------------------------------------------------


def _scripted(tmp_path: Path) -> History:
    history = History(tmp_path / "repo", random.Random(0), skew=False)
    return history


def _assert_same(history: History, frozen: str, *, head: str = "") -> str:
    reference = gitdiff.review_branch_base(history.root, frozen, head=head)
    client = review_base.review_branch_base(history.root, frozen, head=head)
    assert client == reference, (frozen, head, reference, client)
    return client


def test_scripted_shapes_agree_and_move_the_base_as_the_rule_says(tmp_path: Path) -> None:
    h = _scripted(tmp_path)
    # Unborn HEAD and a base the clone lacks: the client says use HEAD.
    _assert_missing_base_uses_head(h, "0" * 40)
    root = h.commit()
    trunk = h.commit(root)
    h.lines = {"main": trunk}
    h.feature = h.commit(trunk)
    h.publish()
    h.set_ref("refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    # Linear own work: the base stays where the Review started.
    h.feature = h.commit(h.feature)
    h.publish()
    assert _assert_same(h, trunk) == trunk
    # Pulling integration in moves the base to the merged tip.
    upstream = h.commit(trunk)
    h.lines["main"] = upstream
    h.feature = h.commit(h.feature, upstream)
    h.publish()
    assert _assert_same(h, trunk) == upstream
    # A local main carrying the Review's own work does not count.
    h.set_ref("refs/heads/main", h.feature)
    h.set_ref("refs/heads/origin/main", h.feature)
    assert _assert_same(h, trunk) == upstream
    # Rebasing onto a newer integration tip moves the base to that tip.
    newer = h.commit(upstream)
    h.lines["main"] = newer
    h.feature = h.commit(h.commit(newer))
    h.publish()
    assert _assert_same(h, trunk) == newer
    # upstream/master ahead of origin/main counts when it descends from the base.
    ahead = h.commit(newer)
    h.lines["up_master"] = ahead
    h.feature = h.commit(h.feature, ahead)
    h.publish()
    assert _assert_same(h, trunk) == ahead
    # A force move below the recorded base rewinds to the merge base.
    h.feature = h.commit(root)
    h.publish()
    assert _assert_same(h, trunk) == root
    # A missing base (both implementations fall back) and the empty tree.
    _assert_missing_base_uses_head(h, "0" * 40)
    _assert_missing_base_uses_head(h, f" {'f' * 40} ")
    assert _assert_same(h, EMPTY) in {root, trunk, newer, ahead, upstream}
    # A detached HEAD advances like any other HEAD.
    h.feature = h.commit(ahead)
    h.detached = True
    h.set_ref("refs/heads/feature", h.feature)
    h.repo.set_head(pygit2.Oid(hex=h.feature))
    assert _assert_same(h, trunk) == ahead
    # An unresolvable head, and an unborn branch in a repository with history.
    assert _assert_same(h, trunk, head="no-such-rev") == trunk
    h.repo.set_head("refs/heads/unborn")
    assert _assert_same(h, f" {trunk} ") == trunk
    assert _assert_same(h, EMPTY) == EMPTY


def test_criss_cross_merges_with_several_merge_bases_agree(tmp_path: Path) -> None:
    h = _scripted(tmp_path)
    trunk = h.commit()
    h.lines = {"main": h.commit(trunk)}
    h.feature = h.commit(trunk)
    h.publish()
    for _ in range(3):
        tip, before = h.lines["main"], h.feature
        assert tip is not None
        h.feature = h.commit(before, tip)
        h.lines["main"] = h.commit(tip, before)
        h.publish()
        h.lines["main"] = h.commit(h.lines["main"])
        h.feature = h.commit(h.feature)
        h.publish()
    tip = h.lines["main"]
    assert tip is not None
    bases = _git(h.root, "merge-base", "--all", "HEAD", tip).stdout.split()
    assert len(bases) > 1, "the history must have several merge bases"
    for frozen in (trunk, *bases, h.feature):
        _assert_same(h, frozen)


# ---------------------------------------------------------------------------
# capture contract: "" means use HEAD
# ---------------------------------------------------------------------------


def test_capture_base_uses_head_when_the_rule_cannot_apply(tmp_path: Path) -> None:
    h = _scripted(tmp_path)
    unborn = review_base.capture_review_base(h.root, "0" * 40)
    assert unborn == ""
    trunk = h.commit()
    h.lines = {"main": h.commit(trunk)}
    h.feature = h.commit(trunk)
    h.publish()
    h.set_ref("refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    assert review_base.capture_review_base(h.root, "") == ""
    assert review_base.capture_review_base(h.root, "  ") == ""
    assert review_base.capture_review_base(h.root, f"{h.rng.getrandbits(160):040x}") == ""
    assert review_base.capture_review_base(h.root, "--output=x") == ""
    assert review_base.capture_review_base(h.root, trunk) == trunk
    assert review_base.capture_review_base(h.root, trunk[:10]) == trunk
    # The empty tree advances to the integration point when there is one.
    assert review_base.capture_review_base(h.root, EMPTY) == trunk
    h.lines = {}
    h.publish()
    assert review_base.capture_review_base(h.root, EMPTY) == EMPTY
    # HEAD unrelated to the recorded base: the base stays, as a full commit id.
    h.feature = h.commit()
    h.publish()
    assert review_base.review_branch_base(h.root, trunk[:10]) == trunk[:10]
    assert review_base.capture_review_base(h.root, trunk[:10]) == trunk
    h.repo.set_head(pygit2.Oid(hex=h.feature))
    assert review_base.capture_review_base(h.root, trunk) == ""
    assert review_base.capture_review_base(tmp_path / "absent", trunk) == ""


def test_capture_base_agrees_with_the_rule_on_generated_histories(tmp_path: Path) -> None:
    for seed in range(10):
        rng = random.Random(1000 + seed)
        root = tmp_path / f"h{seed}"
        _run_history(root, 1000 + seed)
        known = {line.split()[0] for line in _git(root, "rev-list", "--all").stdout.splitlines() if line.strip()}
        attached = _git(root, "symbolic-ref", "-q", "HEAD").returncode == 0
        for frozen in rng.sample(sorted(known), min(5, len(known))):
            captured = review_base.capture_review_base(root, frozen)
            if not attached:
                assert captured == ""
                continue
            expected = gitdiff.review_branch_base(root, frozen)
            assert captured == expected, (seed, frozen)
