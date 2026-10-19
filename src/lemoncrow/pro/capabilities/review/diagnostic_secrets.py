"""Configured secret values and the one matcher that finds them (RC10).

The diagnostic bundle shows operator-chosen names (model ids, endpoint names)
and error text. A configured secret must not appear in either, whatever shape
it is written in. One matcher serves both, so the error-text scrub and the
name checks cannot disagree:

* :func:`match_key` folds text for comparison only: NFKD with every combining
  mark dropped (so ``u`` + U+0301, ``\u00fa`` and ``u`` compare equal), width
  forms, case, whitespace, control and zero-width characters, and a small table
  of Cyrillic, Greek and Latin lookalikes. A secret that a line break, a space,
  an accent or a lookalike letter splits or disguises still folds to the same
  key, and every key character remembers which source character it came from,
  so a match is replaced in the original text. :func:`decode_escapes` undoes
  ``%XX`` and ``\\uXXXX`` escapes first.
* :class:`SecretGuard` is the set of configured secret values: the values of
  secret-named environment variables, the password (or an opaque user) and
  the secret-named query values of URLs found in any environment value, and
  whatever the caller adds (the server's client secrets, ``providers.json``
  keys, the contents of its secret files). A *name* overlaps when it equals,
  contains or is contained in one of them, or shares a run of
  ``piece_chars`` characters with one. A value of a variable whose name is not
  secret-like is not a secret: ``anthropic`` or ``verify-full`` stay readable.

Surfaces differ in who reads the output. Locally the reader owns the
environment, so the guard is thorough: short names inside a secret and
six-character fragments count. A hosted bundle is read by a tenant who also
controls some of the text (a model or endpoint name, an error echoing a
request); every extra rule that answers "does this guess occur in a server
secret" is a guessing oracle, so a ``tenant_visible`` guard answers only for
whole values and for eight-character overlaps.

lc-debt: lookalike folding is a small Cyrillic, Greek and Latin table, not the
full Unicode confusables set. Secrets are the environment, the caller's extras
and the files the server reads; a secret kept only in a vault is not known. A
secret written in chunks of five characters or fewer, or with punctuation
between its characters, is not matched (a chunk that short is indistinguishable
from ordinary text). Upgrade path: generate the table from confusables.txt and
read the operator's secret store.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import parse_qsl, unquote, urlsplit

#: Shortest folded secret a name is compared with, and the shortest value of a secret-named
#: variable that is a secret at all. Below it a value is a word or a flag, and treating it as a
#: secret would blank ordinary text; at five characters ``abc12`` (a short password) is covered.
MIN_SECRET_CHARS: Final[int] = 5
#: Shortest credential taken out of a URL (a password is a credential however short).
MIN_URL_SECRET_CHARS: Final[int] = 4
#: Shortest folded name that counts as "contained in a secret" (a two-letter name is vocabulary, not a leak).
MIN_NAME_CHARS: Final[int] = 3
#: A name shares this many consecutive folded characters with a secret: a partial overlap.
PIECE_CHARS: Final[int] = 6
#: The same two floors for a guard whose output a tenant reads: guesses shorter than this
#: are not answered, so the guard cannot be used to recover a secret a few characters at a time.
TENANT_FLOOR_CHARS: Final[int] = 8

#: Environment variable names whose values are secrets.
_SECRET_NAME_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(key|token|secret|passw|credential|bearer|cookie|dsn|jwt|signature|signing|private|pwd)"
    r"|(?:^|[^a-z])(?:auth|pass|pat|pw)(?:$|[^a-z])"
)
#: URL query parameters whose values are secrets (``sslmode=require`` and ``application_name`` are not).
_SECRET_QUERY_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)key|token|secret|passw|pwd|credential|auth|jwt|bearer|sig|session|code|cookie"
)
#: Values that are settings, not secrets, whatever their variable is called (``AUTH_MODE=enforced``).
_NOT_SECRET_VALUES: Final[frozenset[str]] = frozenset(
    """true false yes no on off enabled disabled enable disable enforced enforce report required require
    optional none null auto default strict verify-full verify-ca prefer allow""".split()
)
_URL_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z][A-Za-z0-9+.-]{1,15}://[^\s'\"<>]+")

#: Cyrillic and Greek letters that read as a Latin letter (folded after ``casefold``).
_LOOKALIKES: Final[Mapping[str, str]] = {
    # Cyrillic
    "\u0430": "a",
    "\u0435": "e",
    "\u043e": "o",
    "\u0440": "p",
    "\u0441": "c",
    "\u0443": "y",
    "\u0445": "x",
    "\u043a": "k",
    "\u043c": "m",
    "\u043d": "h",
    "\u0442": "t",
    "\u0432": "b",
    "\u0456": "i",
    "\u0458": "j",
    "\u0455": "s",
    "\u0501": "d",
    "\u04cf": "l",
    "\u0261": "g",
    # Greek
    "\u03b1": "a",
    "\u03b2": "b",
    "\u03b5": "e",
    "\u03b6": "z",
    "\u03b7": "n",
    "\u03b9": "i",
    "\u03ba": "k",
    "\u03bc": "m",
    "\u03bd": "v",
    "\u03bf": "o",
    "\u03c1": "p",
    "\u03c4": "t",
    "\u03c5": "u",
    "\u03c7": "x",
    # Latin letters NFKD does not decompose
    "\u0131": "i",
    "\u00f8": "o",
    "\u0142": "l",
    "\u0111": "d",
    "\u0127": "h",
}


def match_key(text: str) -> tuple[str, list[int]]:
    """``(key, origins)``: *text* folded for matching, and for each key character its index in *text*.

    Dropped: whitespace, control and format (zero-width) characters, and every
    combining mark (NFKD first, so an accented letter, composed or not, folds to
    its base letter). Folded: width forms, case, :data:`_LOOKALIKES`.
    ``origins`` never decreases, so a key slice maps to the source span
    ``origins[start] .. origins[end - 1] + 1``.
    """

    out: list[str] = []
    origins: list[int] = []
    for index, char in enumerate(text):
        for piece in unicodedata.normalize("NFKD", char):
            category = unicodedata.category(piece)
            if category[0] in "ZC" or category in {"Mn", "Me"}:
                continue
            for folded in piece.casefold():
                out.append(_LOOKALIKES.get(folded, folded))
                origins.append(index)
    return "".join(out), origins


_PERCENT_RE: Final[re.Pattern[str]] = re.compile(r"(?:%[0-9A-Fa-f]{2})+")
_ESCAPE_RE: Final[re.Pattern[str]] = re.compile(r"\\u([0-9A-Fa-f]{4})|\\U([0-9A-Fa-f]{8})|\\x([0-9A-Fa-f]{2})")


def _unescape(match: re.Match[str]) -> str:
    digits = next(group for group in match.groups() if group is not None)
    code = int(digits, 16)
    return chr(code) if code <= 0x10FFFF else match.group()


def decode_escapes(text: str, *, rounds: int = 2) -> str:
    """*text* with ``%XX`` and ``\\uXXXX`` / ``\\xXX`` escapes decoded (twice, for a doubly encoded value)."""

    for _ in range(rounds):
        decoded = _ESCAPE_RE.sub(_unescape, _PERCENT_RE.sub(lambda found: unquote(found.group()), text))
        if decoded == text:
            break
        text = decoded
    return text


def _opaque_user(user: str) -> bool:
    return len(user) >= 16 and any(char.isdigit() for char in user) and any(char.isalpha() for char in user)


def _url_secrets(value: str) -> Iterable[str]:
    """Credentials embedded in the URLs of *value*: the userinfo password (or an opaque lone user)
    and the values of secret-named query parameters. A user name and ``sslmode=require`` are not secrets."""

    for found in _URL_RE.findall(value):
        try:
            parts = urlsplit(found)
            netloc = parts.netloc
            if "@" in netloc:
                user, sep, password = netloc.rpartition("@")[0].partition(":")
                if sep and password:
                    yield password
                    yield unquote(password)
                elif not sep and _opaque_user(user):
                    yield user
            for name, item in parse_qsl(parts.query, keep_blank_values=False):
                if _SECRET_QUERY_RE.search(name):
                    yield item
        except ValueError:
            continue


def _pieces(secrets: Iterable[str], width: int) -> frozenset[str]:
    return frozenset(secret[index : index + width] for secret in secrets for index in range(len(secret) - width + 1))


@dataclass(frozen=True, slots=True)
class SecretGuard:
    """Folded configured secret values (each with its label); answers whether a name overlaps one.

    ``tenant_visible`` is true when someone other than the operator reads the output: only
    whole values and ``TENANT_FLOOR_CHARS``-character overlaps are answered, and the error-text
    scrub does no fragment or cut-prefix matching (see the module docstring).
    """

    #: Folded secret -> the label error text shows for it (the variable name, or ``secret``).
    labels: Mapping[str, str] = field(default_factory=dict)
    tenant_visible: bool = False
    pieces: frozenset[str] = field(default=frozenset(), init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "pieces", _pieces(self.labels, self.piece_chars))

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(self.labels)

    @property
    def piece_chars(self) -> int:
        return TENANT_FLOOR_CHARS if self.tenant_visible else PIECE_CHARS

    @property
    def contained_floor(self) -> int:
        return TENANT_FLOOR_CHARS if self.tenant_visible else MIN_NAME_CHARS

    @classmethod
    def from_environ(
        cls, environ: Mapping[str, str], extra: Iterable[str] = (), *, tenant_visible: bool = False
    ) -> SecretGuard:
        values: list[tuple[str, int, str]] = [(item, MIN_SECRET_CHARS, "secret") for item in extra if item]
        for name, value in environ.items():
            if not isinstance(value, str) or not value.strip():
                continue
            if _SECRET_NAME_RE.search(name) and value.strip().lower() not in _NOT_SECRET_VALUES:
                values.append((value.strip(), MIN_SECRET_CHARS, name))
            if "://" in value:
                values.extend((item, MIN_URL_SECRET_CHARS, "secret") for item in _url_secrets(value))
        labels: dict[str, str] = {}
        for item, floor, label in values:
            key = match_key(item)[0]
            if len(key) >= floor:
                labels.setdefault(key, label)
        return cls(labels, tenant_visible)

    def with_extra(self, extra: Iterable[str]) -> SecretGuard:
        """This guard plus more secret values (the caller's configured secrets)."""

        labels = dict(self.labels)
        for item in extra:
            key = match_key(item)[0] if item else ""
            if len(key) >= MIN_SECRET_CHARS:
                labels.setdefault(key, "secret")
        return SecretGuard(labels, self.tenant_visible)

    def overlaps(self, text: object) -> bool:
        """Whether *text* equals or contains a configured secret, sits inside one, or shares a run with one."""

        if not isinstance(text, str) or not self.labels:
            return False
        key = match_key(text)[0]
        if not key:
            return False
        floor, width = self.contained_floor, self.piece_chars
        if any(secret in key or (len(key) >= floor and key in secret) for secret in self.labels):
            return True
        return len(key) >= width and any(
            key[index : index + width] in self.pieces for index in range(len(key) - width + 1)
        )


NO_SECRETS: Final[SecretGuard] = SecretGuard()

__all__ = [
    "MIN_NAME_CHARS",
    "MIN_SECRET_CHARS",
    "MIN_URL_SECRET_CHARS",
    "NO_SECRETS",
    "PIECE_CHARS",
    "TENANT_FLOOR_CHARS",
    "SecretGuard",
    "decode_escapes",
    "match_key",
]
