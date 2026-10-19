"""RC11 item 9: the hard-deny path policy is a differential rewrite with no fail-open divergence.

The legacy matcher (path tokens split on ``/ . _ -`` of the lowercased raw
path, root prefixes, exact names) is kept here verbatim as the oracle. Over
randomized and adversarial paths (Unicode compatibility forms, case, ``..``,
``./``, trailing slashes, Windows separators, invisible characters, combining
marks, look-alike letters, symlink-like names) every divergence is
classified:

* **fail-safe** -- the new policy adds a class (auto-approval is withheld more);
* **fail-open** -- the legacy matcher found a class the new policy does not.

No fail-open divergence may exist.
"""

from __future__ import annotations

import random
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import PurePosixPath

import pytest

from lemoncrow.pro.capabilities.review import auto_approval as aa
from lemoncrow.pro.capabilities.review.auto_approval import hard_deny_classes, policy_path
from lemoncrow.pro.capabilities.review.rules import path_matches

# --------------------------------------------------------------------------- #
# the legacy oracle (verbatim behavior of the token heuristic it replaces)
# --------------------------------------------------------------------------- #

_LEGACY_SPLIT = re.compile(r"[/._\-]+")
_LEGACY_PREFIX = {
    ".github/": "infra_ci_release",
    ".gitlab/": "infra_ci_release",
    ".circleci/": "infra_ci_release",
    ".buildkite/": "infra_ci_release",
    ".lemoncrow/": "review_policy",
}
_LEGACY_NAMES = {
    ".gitlab-ci.yml": "infra_ci_release",
    ".gitlab-ci.yaml": "infra_ci_release",
    "azure-pipelines.yml": "infra_ci_release",
    "bitbucket-pipelines.yml": "infra_ci_release",
    ".travis.yml": "infra_ci_release",
    ".goreleaser.yml": "infra_ci_release",
    ".goreleaser.yaml": "infra_ci_release",
    ".envrc": "secrets_crypto",
}


def _legacy_matches(path: str, patterns: Sequence[str]) -> bool:
    try:
        return bool(patterns) and path_matches(path, patterns)
    except ValueError:
        return True


def legacy_hard_deny_classes(paths: Iterable[str], *, sensitive_paths: Sequence[str] = ()) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}

    def add(klass: str, path: str) -> None:
        bucket = found.setdefault(klass, [])
        if path not in bucket:
            bucket.append(path)

    for path in dict.fromkeys(item for item in paths if item):
        tokens = frozenset(token for token in _LEGACY_SPLIT.split(path.lower()) if token)
        lowered = path.lower().lstrip("/")
        name = PurePosixPath(lowered).name
        for klass, words in aa._CLASS_TOKENS.items():
            if tokens & words:
                add(klass, path)
        for prefix, klass in _LEGACY_PREFIX.items():
            if lowered.startswith(prefix):
                add(klass, path)
        if name in _LEGACY_NAMES:
            add(_LEGACY_NAMES[name], path)
        if name.startswith("id_rsa") or name.endswith((".key", ".keystore", ".jks")):
            add("secrets_crypto", path)
        if sensitive_paths and _legacy_matches(path, sensitive_paths):
            add("configured_sensitive", path)
    return found


# --------------------------------------------------------------------------- #
# adversarial generator
# --------------------------------------------------------------------------- #

_CLASS_WORDS = sorted({word for words in aa._CLASS_TOKENS.values() for word in words})
_NEUTRAL = ["docs", "src", "utils", "authoring", "review", "widgets", "index", "lib", "app", "readme", "x"]
_NAMES = [*_LEGACY_NAMES, "id_rsa", "id_rsa.pub", "server.key", "store.jks", "a.keystore", "CODEOWNERS"]
_ROOTS = [".github", ".gitlab", ".circleci", ".buildkite", ".lemoncrow"]
_SEPARATORS = ["/", "\\", "//", "/./", "\\.\\"]
_EXTENSIONS = ["", ".py", ".ts", ".yml", ".md", ".sql", ".key", "/"]
_ZERO_WIDTH = ["\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u00ad"]
_CONFUSABLE = {
    "a": "\u0430",
    "e": "\u0435",
    "o": "\u043e",
    "p": "\u0440",
    "c": "\u0441",
    "x": "\u0445",
    "i": "\u0456",
    "s": "\u0455",
}
_SENSITIVE = [(), ("src/payments/**",), ("SRC/Ledger/**", "deploy/"), ("**/*.pem",)]
#: Names Git stores as ordinary components that fold (NFKC, marks/format dropped, stripped) to ``..``.
_DOT_DOT = [
    "..",
    "\uff0e\uff0e",  # fullwidth full stops
    "\u2025",  # two dot leader
    ".. ",
    " ..",
    ".\u200b.",
    ".\u0301.",
    ".\u00ad.",
    "\ufe52\ufe52",  # small full stops
]


def _fullwidth(text: str) -> str:
    return "".join(chr(ord(char) + 0xFEE0) if "!" <= char <= "~" else char for char in text)


def _mutate(word: str, rng: random.Random) -> str:
    choice = rng.randrange(9)
    if choice == 0:
        return word.upper()
    if choice == 1:
        return word.title()
    if choice == 2:
        return _fullwidth(word)
    if choice == 3 and len(word) > 1:
        cut = rng.randrange(1, len(word))
        return word[:cut] + rng.choice(_ZERO_WIDTH) + word[cut:]
    if choice == 4:
        return "".join(char + ("\u0301" if rng.random() < 0.3 else "") for char in word)
    if choice == 5:
        return "".join(_CONFUSABLE.get(char, char) if rng.random() < 0.5 else char for char in word)
    if choice == 6:
        return word + rng.choice(["Service", "Handler", "2", "_v2", "-old", "+x", " copy", "~"])
    return word


def _random_path(rng: random.Random) -> str:
    parts: list[str] = []
    if rng.random() < 0.2:
        parts.append(rng.choice([".", "..", "", "C:"]))
    if rng.random() < 0.2:
        parts.append(_mutate(rng.choice(_ROOTS), rng))
        if rng.random() < 0.5:
            # A `..` look-alike straight after a class directory: it must never pop the class away.
            parts.append(rng.choice(_DOT_DOT))
    for _ in range(rng.randrange(1, 5)):
        pool = _CLASS_WORDS if rng.random() < 0.5 else _NEUTRAL
        parts.append(_mutate(rng.choice(pool), rng))
        if rng.random() < 0.1:
            parts.append("..")
    if rng.random() < 0.3:
        name = rng.choice(_NAMES)
        parts.append(_mutate(name, rng) if rng.random() < 0.5 else name)
    path = ""
    for index, part in enumerate(parts):
        path += (rng.choice(_SEPARATORS) if index else "") + part
    path += rng.choice(_EXTENSIONS)
    if rng.random() < 0.05:
        path += " -> ../src/auth"  # a symlink-like name
    return path


_CURATED = [
    "src/auth/session.py",
    "SRC/AUTH/SESSION.PY",
    "src\\auth\\session.py",
    "./src/auth/",
    "src//auth///x",
    "src/./auth/../auth/x.py",
    "docs/../.github/workflows/ci.yml",
    "src/auth/../../docs/a.md",
    "../.github/workflows/ci.yml",
    "/.github/workflows/ci.yml",
    "\uff53\uff52\uff43/\uff41\uff55\uff54\uff48/x.py",
    "src/au\u200bth/x.py",
    "src/a\u0301uth/x.py",
    "src/\u0430uth/x.py",
    "src/AuthService.ts",
    "src/GraphQL/schema.ts",
    "lib/OAuth2Client.java",
    "x/link -> ../src/auth",
    ".GitHub/Workflows/CI.yml",
    "nested/.github/workflows/ci.yml",
    "keys/ID_RSA",
    "certs\\server.KEY",
    "config/.ENV.production",
    "src/payments/x.py",
    "Src/Payments/x.py",
    "docs/authoring.md",
    "src/review_actions.py",
    "tests/test_widgets.py",
    "",
    ".",
    "a\x00b",
    "C:\\repo\\auth\\x",
    # The adversarial review's look-alikes of `..` after a class directory.
    *(f"{root}/{dots}/policy.yml" for root in _ROOTS for dots in _DOT_DOT),
    *(f"{root}{sep}..{sep}x.md" for root in _ROOTS for sep in ("\\", "/")),
    ".github/workflows/..\\..\\x.yml",
    "x/.envrc/..\\y",
    ".envrc\\..\\y",
    "server.key\\..\\y",
    "src\\..\\.envrc",
]


def _diff(paths: Iterable[str], sensitive: Sequence[str]) -> tuple[Counter[str], list[tuple[str, set[str]]]]:
    """``(divergence counts by class, fail-open cases)`` of the new policy against the oracle."""

    counts: Counter[str] = Counter()
    fail_open: list[tuple[str, set[str]]] = []
    for path in paths:
        if not path:
            continue
        old = set(legacy_hard_deny_classes([path], sensitive_paths=sensitive))
        new = set(hard_deny_classes([path], sensitive_paths=sensitive))
        counts["paths"] += 1
        if old == new:
            counts["identical"] += 1
            continue
        lost = old - new
        if lost:
            fail_open.append((path, lost))
            counts["fail_open"] += 1
        for klass in sorted(new - old):
            counts[f"fail_safe:{klass}"] += 1
        if new - old:
            counts["fail_safe"] += 1
            if not old:
                counts["fail_safe:newly_denied"] += 1
    return counts, fail_open


def differential_report(*, seed: int = 20261007, samples: int = 20000) -> Mapping[str, int]:
    rng = random.Random(seed)
    total: Counter[str] = Counter()
    open_cases: list[tuple[str, set[str]]] = []
    for sensitive in _SENSITIVE:
        paths = [*_CURATED, *(_random_path(rng) for _ in range(samples // len(_SENSITIVE)))]
        counts, fail_open = _diff(paths, sensitive)
        total.update(counts)
        open_cases.extend(fail_open)
    assert not open_cases, f"fail-open divergences: {open_cases[:10]}"
    return dict(total)


def test_the_typed_path_policy_has_no_fail_open_divergence_from_the_token_heuristic() -> None:
    report = differential_report()
    assert report["paths"] >= 20000
    assert report.get("fail_open", 0) == 0
    # The rewrite is not a no-op: normalization finds classes the heuristic missed.
    assert report["fail_safe"] > 0 and report["fail_safe:newly_denied"] > 0


@pytest.mark.parametrize(
    ("path", "klass"),
    [
        ("SRC/AUTH/SESSION.PY", "auth"),
        ("src\\auth\\session.py", "auth"),
        ("\uff53\uff52\uff43/\uff41\uff55\uff54\uff48/x.py", "auth"),
        ("src/au\u200bth/x.py", "auth"),
        ("src/a\u0301uth/x.py", "auth"),
        ("src/\u0430uth/x.py", "auth"),
        ("src/AuthService.ts", "auth"),
        ("nested/.github/workflows/ci.yml", "infra_ci_release"),
        ("docs\\..\\.github\\workflows\\ci.yml", "infra_ci_release"),
        ("certs\\server.KEY", "secrets_crypto"),
        ("x/link -> ../src/auth", "auth"),
        # A name a `..` popped still counts: the change touched it under that name.
        ("x/.envrc/..\\y", "secrets_crypto"),
        ("server.key\\..\\y", "secrets_crypto"),
        ("../outside.txt", "unsafe_path"),
        ("/etc/passwd", "unsafe_path"),
        ("C:\\repo\\x.txt", "unsafe_path"),
    ],
)
def test_normalization_catches_what_the_token_heuristic_missed(path: str, klass: str) -> None:
    assert klass in hard_deny_classes([path])


def test_policy_named_sensitive_globs_match_the_normalized_and_case_folded_path() -> None:
    for path in ("Src/Payments/x.py", "src\\payments\\x.py", "./src/payments/x.py", "src/./payments/x.py"):
        assert "configured_sensitive" in hard_deny_classes([path], sensitive_paths=("src/payments/**",)), path


def test_a_policy_path_is_lexically_resolved_and_folded() -> None:
    resolved = policy_path("Docs\\.\\Guide\\..\\\uff21uth\\")
    # Git stores no `..` component: resolved for matching, but never proof of a safe path.
    assert resolved.segments == ("docs", "auth") and resolved.unsafe is True
    assert policy_path("Docs/./Guide/\uff21uth/").unsafe is False
    assert policy_path("a/../../b").unsafe is True
    assert policy_path("   ").unsafe is True


def test_an_excluded_path_cannot_escape_the_scope_by_spelling() -> None:
    from tests.gateway.test_review_auto_approval import _decide, _failed, _policy

    policy = _policy(exclude_paths=("src/payments/**",))
    for path in ("src\\payments\\x.py", "SRC/Payments/x.py", "./src/payments/x.py"):
        gate = _failed(_decide(policy=policy, changed_paths=("docs/a.md", path)))["scope"]
        assert "excluded_path" in gate.reason, path


@pytest.mark.parametrize("dots", _DOT_DOT)
@pytest.mark.parametrize(("root", "klass"), [(root, aa._SEGMENT_CLASSES[root]) for root in _ROOTS])
def test_a_dot_dot_look_alike_never_pops_a_class_directory_away(root: str, dots: str, klass: str) -> None:
    """RC11 fix round: `.lemoncrow/\uff0e\uff0e/policy.yml` is a real file under `.lemoncrow` to Git."""

    for path in (f"{root}/{dots}/policy.yml", f"{root.upper()}/{dots}/x", f"{root}\\{dots}\\x"):
        classes = hard_deny_classes([path])
        assert klass in classes and "unsafe_path" in classes, (path, classes)
        assert policy_path(path).unsafe is True
