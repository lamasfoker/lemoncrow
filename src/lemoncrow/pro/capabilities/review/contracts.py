"""Contract fingerprints for HumanDecision semantic scope (spec §5, Phase 3b).

A decision's ``contract_scope`` names the public or behavioral contracts it
relies on. A reference of the form ``<path>::<symbol>`` (or ``<path>#<symbol>``,
``Class.method`` allowed) is analyzed with tree-sitter: the fingerprint covers
the declaration header -- decorators, name, parameters, return type,
receiver -- and never the body, so a body-only edit stays an implementation
change. Declarations whose body *is* the contract (interfaces, type aliases,
structs, enums, traits) are fingerprinted whole.

Outcomes are explicit and never raise:

- ``sha256:<hex>``: the analyzed signature.
- ``absent``: the file parses and defines no such symbol (a real difference).
- ``unavailable``: the file cannot be read or its language is unsupported;
  validity then reports *unverified*, never reconsideration.
- ``unanalyzed:<reference>``: free text with no path. Stable, and disclosed as
  not analyzed rather than making every acceptance permanently unverified.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import yaml

from lemoncrow.pro.capabilities.review.routine import routine_language

CONTRACT_VERSION = 1
UNAVAILABLE = "unavailable"
ABSENT = "absent"
UNANALYZED_PREFIX = "unanalyzed:"

_MAX_BYTES = 1 << 20
_WHITESPACE = re.compile(r"\s+")
# The declaration's body is its contract: fingerprint the whole node.
_WHOLE = frozenset(
    {
        "interface_declaration",
        "type_alias_declaration",
        "enum_declaration",
        "type_spec",
        "type_declaration",
        "struct_item",
        "enum_item",
        "trait_item",
        "type_item",
        "record_declaration",
        "annotation_type_declaration",
    }
)


def split_reference(reference: str) -> tuple[str, str] | None:
    """``(path, symbol)`` for an analyzable reference, else ``None``."""

    for separator in ("::", "#"):
        if separator in reference:
            path, _, symbol = reference.partition(separator)
            path, symbol = path.strip(), symbol.strip()
            if path and symbol:
                return path, symbol
    return None


def _text(source: bytes, node: Any) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _name(source: bytes, node: Any) -> str:
    for field in ("name", "type"):
        child = node.child_by_field_name(field)
        if child is not None:
            return _text(source, child)
    return ""


def _walk(node: Any) -> Iterator[Any]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


def _in_container(source: bytes, node: Any, container: str) -> bool:
    receiver = node.child_by_field_name("receiver")
    if receiver is not None and re.search(rf"\b{re.escape(container)}\b", _text(source, receiver)):
        return True  # Go method on the named receiver type
    parent = node.parent
    while parent is not None:
        if parent.type not in ("module", "program", "source_file") and _name(source, parent) == container:
            return True
        parent = parent.parent
    return False


def _signature(source: bytes, node: Any) -> str:
    start = node.start_byte
    if node.parent is not None and node.parent.type == "decorated_definition":
        start = node.parent.start_byte  # decorators change the contract
    if node.type in _WHOLE:
        end = node.end_byte
    else:
        body = node.child_by_field_name("body")
        value = node.child_by_field_name("value")
        if body is None and value is not None:
            body = value.child_by_field_name("body")  # const f = (a) => { ... }
        end = body.start_byte if body is not None else node.end_byte
    return _WHITESPACE.sub(" ", source[start:end].decode("utf-8", "replace")).strip()


def _defines(node: Any) -> bool:
    kind = str(node.type)
    return kind.endswith(("_definition", "_declaration", "_item", "_spec")) or kind == "variable_declarator"


def contract_fingerprint(
    reference: str,
    read_new: Callable[[str], str | None],
    *,
    deleted_paths: Collection[str] = (),
) -> str:
    """Fingerprint one contract reference against the selected Snapshot.

    A file the Snapshot deletes (or renames away) has no declaration: that is
    ``absent``, a real change, not ``unavailable``.
    """

    parts = split_reference(reference)
    if parts is None:
        return UNANALYZED_PREFIX + reference.strip()
    path, symbol = parts
    if path in deleted_paths:
        return ABSENT
    language = routine_language(path)
    if not language:
        return UNAVAILABLE
    try:
        text = read_new(path)
    except (OSError, ValueError):
        return UNAVAILABLE
    if text is None:
        return UNAVAILABLE
    source = text.encode("utf-8")
    if len(source) > _MAX_BYTES:
        return UNAVAILABLE
    try:
        from tree_sitter_language_pack import get_parser

        tree = get_parser(language).parse(source)  # type: ignore[arg-type]
    except Exception:  # any parser failure means "not analyzable now"
        return UNAVAILABLE
    container, _, leaf = symbol.rpartition(".")
    signatures = sorted(
        {
            _signature(source, node)
            for node in _walk(tree.root_node)
            if _defines(node)
            and node.child_by_field_name("name") is not None
            and _text(source, node.child_by_field_name("name")) == leaf
            and (not container or _in_container(source, node, container))
        }
    )
    if not signatures:
        return ABSENT
    digest = hashlib.sha256("\n".join(signatures).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


# --------------------------------------------------------------------------- #
# Static side-effect inventory (Phase 9R items 9 and 34)
# --------------------------------------------------------------------------- #
#
# The external surface one file *states*, read from its text and never executed:
# environment variables, config keys, outbound hosts, routes/endpoints, CLI
# flags, database tables, destructive database operations and migrations,
# permissions/scopes, feature flags, scheduled jobs, telemetry events,
# subprocesses, filesystem paths outside the repository, and personal or secret
# fields reaching a log, telemetry, a third-party SDK or an external endpoint.
# Dependencies and install scripts come from ``dependencies.py``.
#
# ``category`` + ``name`` is the identity the side-effect register counts;
# ``value`` separates a changed fact (an edited migration, a raised permission
# level) from an unchanged one; ``line`` and ``detail`` are display only.
# Categories whose identity is per location (destructive operations, migrations,
# scheduled jobs, data flows, workflow permissions) carry the path in ``name``.
#
# lc-debt: line-local regexes over one line (data flows: up to four) miss
# multi-line calls, aliased imports and values built at runtime. Upgrade path:
# per-language tree-sitter queries, prioritized by sampled false negatives.

SIDE_EFFECT_INVENTORY_VERSION = 1
"""Bump on any change to what the inventory extracts or how it names a fact."""

SIDE_EFFECT_CATEGORIES: tuple[str, ...] = (
    "env_var",
    "config_key",
    "outbound_host",
    "route",
    "cli_flag",
    "db_table",
    "db_drop",
    "migration",
    "permission",
    "feature_flag",
    "scheduled_job",
    "telemetry_event",
    "subprocess",
    "filesystem_path",
    "data_flow",
    "dependency",
    "install_script",
)


@dataclass(frozen=True, slots=True)
class SideEffectFact:
    """One external-surface fact a file states."""

    category: str
    name: str
    path: str
    line: int = 0
    value: str = ""
    detail: str = ""

    @property
    def identity(self) -> str:
        return f"{self.category}:{self.name}"


_INV_MAX_BYTES = 1 << 20
_INV_MAX_LINE = 2000
_INV_MAX_NAME = 200
_COMMENT_LINE = re.compile(r"^\s*(?:#(?![!\[])|//|/\*|\*|<!--|--\s|;|rem\s)", re.IGNORECASE)
_CODE_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".mts",
        ".cts",
        ".go",
        ".rs",
        ".rb",
        ".java",
        ".kt",
        ".kts",
        ".scala",
        ".cs",
        ".php",
        ".swift",
        ".sh",
        ".bash",
        ".zsh",
        ".ex",
        ".exs",
    }
)
_CONFIG_SUFFIXES = frozenset(
    {".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".properties", ".tf", ".hcl", ".xml"}
)
_CODE_BASENAMES = frozenset({"Dockerfile", "Makefile", "Procfile", "Jenkinsfile", "Rakefile", "Gemfile"})
# Package metadata (homepage, repository, bug tracker) is not a call.
_METADATA_BASENAMES = frozenset(
    {"package.json", "pyproject.toml", "Cargo.toml", "setup.cfg", "setup.py", "go.mod", "composer.json", "Gemfile"}
)
_URL_METADATA_MARKERS = (
    "homepage",
    "repository",
    "bugs",
    "documentation",
    "license",
    "$schema",
    "xmlns",
    "schemalocation",
    "@see",
    "@link",
    "changelog",
    "funding",
)

_ENV_NAME = r"([A-Za-z_][A-Za-z0-9_]{0,127})"
_ENV_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"""\bos\.environ(?:\.(?:get|setdefault|pop))?\s*[\[(]\s*["']""" + _ENV_NAME + "[\"']"),
    re.compile(r"""\bgetenv\s*\(\s*["']""" + _ENV_NAME + "[\"']"),
    re.compile(r"""\b(?:process\.env|import\.meta\.env)\.""" + _ENV_NAME),
    re.compile(r"""\bprocess\.env\s*\[\s*["'`]""" + _ENV_NAME + "[\"'`]"),
    re.compile(r"""\bDeno\.env\.get\s*\(\s*["']""" + _ENV_NAME + "[\"']"),
    re.compile(r"""\bos\.(?:Getenv|LookupEnv)\s*\(\s*\"""" + _ENV_NAME + '"'),
    re.compile(r"""\benv::var(?:_os)?\s*\(\s*\"""" + _ENV_NAME + '"'),
    re.compile(r"""\b(?:option_)?env!\s*\(\s*\"""" + _ENV_NAME + '"'),
    re.compile(r"""\bENV(?:\.fetch)?\s*[\[(]\s*["']""" + _ENV_NAME + "[\"']"),
)
_DOCKER_ENV = re.compile(r"^\s*(?:ENV|ARG)\s+" + _ENV_NAME)
_ENV_FILE_LINE = re.compile(r"^\s*(?:export\s+)?" + _ENV_NAME + r"\s*=")

_CONFIG_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsettings\.([A-Z][A-Z0-9_]{2,127})\b"),
    re.compile(r"""\bgetattr\s*\(\s*settings\s*,\s*["']([A-Z][A-Z0-9_]{1,127})["']"""),
    re.compile(r"""\bconfig\s*\[\s*["']([\w.-]{2,127})["']\s*\]"""),
    re.compile(r"""\bconfig\.(?:get|has|getboolean|getint)\s*\(\s*["']([\w.-]{2,127})["']"""),
    re.compile(r"""\bviper\.(?:Get\w*|IsSet)\s*\(\s*"([\w.-]{2,127})"\s*\)"""),
    re.compile(r"""@Value\s*\(\s*"\$\{([\w.-]{2,127})"""),
)

_HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?"
_URL_RE = re.compile(r"(?:https?|wss?)://(?:[^\s/@\"'`<>]+@)?(" + _HOST_LABEL + r"(?:\." + _HOST_LABEL + r")+)", re.I)
_IPV4 = re.compile(r"^(\d{1,3})\.(\d{1,3})\.\d{1,3}\.\d{1,3}$")
_NON_EXTERNAL_HOSTS = frozenset(
    {
        "json-schema.org",
        "www.w3.org",
        "w3.org",
        "schemas.xmlsoap.org",
        "schemas.microsoft.com",
        "purl.org",
        "xmlns.com",
        "spdx.org",
        "opensource.org",
        "creativecommons.org",
        "www.apache.org",
    }
)
_NON_EXTERNAL_SUFFIXES = (
    "localhost",
    ".local",
    ".test",
    ".example",
    ".invalid",
    ".internal",
    ".lan",
    ".home.arpa",
    ".svc",
    "example.com",
    "example.org",
    "example.net",
)

_ROUTE_RES: tuple[tuple[re.Pattern[str], int, int, frozenset[str]], ...] = (
    # (pattern, method group (0 = any), path group, suffixes; empty = all code)
    (
        re.compile(
            r"""^\s*@\s*[A-Za-z_][\w.]*\.(get|post|put|patch|delete|head|options|route|api_route|websocket)"""
            r"""\s*\(\s*["'](/[^"']*)["']"""
        ),
        1,
        2,
        frozenset({".py"}),
    ),
    (
        re.compile(
            r"""\b(?:app|router|server|fastify|hono|routes)\s*\.\s*(get|post|put|patch|delete|all|head|options)"""
            r"""\s*\(\s*["'`](/[^"'`]*)["'`]"""
        ),
        1,
        2,
        frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}),
    ),
    (
        re.compile(
            r"""\b[A-Za-z_]\w*\.(HandleFunc|Handle|GET|POST|PUT|PATCH|DELETE|Get|Post|Put|Patch|Delete|Any)"""
            r"""\s*\(\s*"(/[^"]*)\""""
        ),
        1,
        2,
        frozenset({".go"}),
    ),
    (
        re.compile(
            r"""@(Get|Post|Put|Patch|Delete|Request)Mapping\s*\(\s*(?:(?:value|path)\s*=\s*)?\{?\s*"([^"]*)\""""
        ),
        1,
        2,
        frozenset({".java", ".kt"}),
    ),
    (re.compile(r"""#\[(get|post|put|patch|delete)\s*\(\s*"(/[^"]*)\""""), 1, 2, frozenset({".rs"})),
    (
        re.compile(r"""\.route\s*\(\s*"(/[^"]*)"\s*,\s*(get|post|put|patch|delete|any)\s*\("""),
        2,
        1,
        frozenset({".rs"}),
    ),
    (re.compile(r"""^\s*(get|post|put|patch|delete)\s+["'](/?[^"']*)["']"""), 1, 2, frozenset({".rb"})),
)
_DJANGO_PATH = re.compile(r"""\b(?:re_)?path\s*\(\s*r?["']([^"']*)["']\s*,""")
_FILE_ROUTES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:^|/)app/(.*?)/?route\.(?:ts|js|mjs)$"),
    re.compile(r"(?:^|/)pages/(api(?:/.*?)?)(?:/index)?\.(?:ts|tsx|js|jsx|mjs)$"),
    re.compile(r"(?:^|/)routes/(.*?)/?\+server\.(?:ts|js)$"),
)
_ANY_METHODS = frozenset({"ROUTE", "API_ROUTE", "ALL", "ANY", "HANDLE", "HANDLEFUNC", "REQUEST", "WEBSOCKET"})

_CLI_CALL = re.compile(r"\b(?:add_argument|click\.option|typer\.Option|option|requiredOption|Option)\s*\(")
_CLI_DASHED = re.compile(r"(?<![\w-])--([A-Za-z0-9][\w-]{0,63})")
_CLI_NAMED: tuple[tuple[re.Pattern[str], frozenset[str]], ...] = (
    (
        re.compile(
            r"""\bflag\.(?:Bool|Int|Int64|Uint|Uint64|String|Float64|Duration|Var|Func|TextVar)(?:Var)?"""
            r"""\s*\(\s*(?:&?[\w.]+\s*,\s*)?"([A-Za-z0-9][\w-]{0,63})\""""
        ),
        frozenset({".go"}),
    ),
    (re.compile(r"""Flags\(\)\.\w+\s*\(\s*(?:&?[\w.]+\s*,\s*)?"([A-Za-z0-9][\w-]{0,63})\""""), frozenset({".go"})),
    (re.compile(r"""#\[(?:arg|clap)\([^)\]]*\blong\s*=\s*"([A-Za-z0-9][\w-]{0,63})\""""), frozenset({".rs"})),
    (re.compile(r"""\.long\s*\(\s*"([A-Za-z0-9][\w-]{0,63})"\s*\)"""), frozenset({".rs"})),
)

_MIGRATION_PATH = re.compile(
    r"(?:^|/)(?:migrations?|alembic/versions|db/migrate|prisma/migrations|flyway|liquibase|changelogs?)/", re.I
)
_DDL_TABLE = r"""[`"\[]?([A-Za-z_][\w.]*)"""
_SQL_CREATE = r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:GLOBAL|LOCAL)\s+)?(?:TEMP(?:ORARY)?\s+|UNLOGGED\s+)?TABLE\s+"
_SQL_RES: tuple[tuple[str, str], ...] = (
    ("create", _SQL_CREATE + r"(?:IF\s+NOT\s+EXISTS\s+)?" + _DDL_TABLE),
    ("drop_table", r"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?" + _DDL_TABLE),
    (
        "drop_column",
        r"\bALTER\s+TABLE\s+(?:ONLY\s+)?(?:IF\s+EXISTS\s+)?"
        + _DDL_TABLE
        + r"""[`"\]]?\s+DROP\s+(?:COLUMN\s+)?(?:IF\s+EXISTS\s+)?[`"\[]?([A-Za-z_]\w*)""",
    ),
    ("truncate", r"\bTRUNCATE\s+(?:TABLE\s+)?" + _DDL_TABLE),
)
# Case-insensitive inside .sql files; upper-case keywords only inside code.
_SQL_FILE_RES = tuple((op, re.compile(pattern, re.I)) for op, pattern in _SQL_RES)
_SQL_CODE_RES = tuple((op, re.compile(pattern)) for op, pattern in _SQL_RES)
_ORM_TABLES: tuple[re.Pattern[str], ...] = (
    re.compile(r"""\b__tablename__\s*=\s*["']([A-Za-z_]\w*)["']"""),
    re.compile(r"""\bdb_table\s*=\s*["']([A-Za-z_]\w*)["']"""),
    re.compile(r"""\bop\.create_table\s*\(\s*["']([A-Za-z_]\w*)["']"""),
    re.compile(r"\bcreate_table\s*\(?\s*:([A-Za-z_]\w*)"),
    re.compile(r"""\bmigrations\.CreateModel\s*\(\s*name\s*=\s*["']([A-Za-z_]\w*)["']"""),
    re.compile(r"""\bcreateTable\s*\(\s*["']([A-Za-z_]\w*)["']"""),
)
_ORM_DROPS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("drop_table", re.compile(r"""\bop\.drop_table\s*\(\s*["']([A-Za-z_]\w*)["']""")),
    ("drop_column", re.compile(r"""\bop\.drop_column\s*\(\s*["']([A-Za-z_]\w*)["']\s*,\s*["']([A-Za-z_]\w*)["']""")),
    ("drop_table", re.compile(r"\bdrop_table\s*\(?\s*:([A-Za-z_]\w*)")),
    ("drop_column", re.compile(r"\bremove_column\s*\(?\s*:([A-Za-z_]\w*)\s*,\s*:([A-Za-z_]\w*)")),
    ("drop_table", re.compile(r"""\bmigrations\.DeleteModel\s*\(\s*name\s*=\s*["']([A-Za-z_]\w*)["']""")),
    (
        "drop_column",
        re.compile(
            r"""\bmigrations\.RemoveField\s*\(\s*model_name\s*=\s*["']([A-Za-z_]\w*)["']\s*,"""
            r"""\s*name\s*=\s*["']([A-Za-z_]\w*)["']"""
        ),
    ),
    ("drop_table", re.compile(r"""\bdropTable(?:IfExists)?\s*\(\s*["']([A-Za-z_]\w*)["']""")),
)
_PRISMA_MODEL = re.compile(r"^\s*model\s+([A-Za-z_]\w*)\s*\{")

_IAM_KEY = re.compile(r"\b(?:Action|NotAction|actions)\b")
_IAM_TOKEN = re.compile(r"""["']([a-z0-9][a-z0-9-]{1,40}:[A-Za-z0-9*][A-Za-z0-9*]{0,80})["']""")
_ANDROID_PERMISSION = re.compile(r"""<uses-permission[^>]*?android:name\s*=\s*"([^"]{1,120})\"""")
_SCOPE_LINE = re.compile(r"""\bscopes?\b["']?\s*[:=(]\s*(.*)$""", re.I)
_QUOTED = re.compile(r"""["']([^"']{2,200})["']""")
_KNOWN_SCOPES = frozenset({"openid", "offline_access"})

_FLAG_RES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"""\b(?:variation|boolVariation|stringVariation|bool_variation|variation_detail|isEnabled|is_enabled|"""
        r"""isOn|is_on|isFeatureEnabled|is_feature_enabled|getFeatureFlag|get_feature_flag|feature_enabled|"""
        r"""featureEnabled|useFlag|useFeatureFlag|useFeatureIsOn|getTreatment|get_treatment|checkGate|check_gate)"""
        r"""\s*\(\s*(?:[A-Za-z_][\w.]*\s*,\s*)?["'`]([\w.:-]{2,100})["'`]"""
    ),
    re.compile(r"\bFlipper\.enabled\?\s*\(\s*:(\w{2,100})"),
    re.compile(r"""\bflags?\s*\.\s*(?:get|enabled|is_enabled|isEnabled)\s*\(\s*["']([\w.:-]{2,100})["']"""),
)

_SCHEDULE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bcrontab\s*\(([^)]{0,200})\)"),
    re.compile(r"@\s*(?:[\w.]+\.)?(?:scheduled_job|periodic_task|cron|schedule)\s*\(([^)]{0,200})\)"),
    re.compile(r"""\bcron\.schedule\s*\(\s*["'`]([^"'`]{1,100})["'`]"""),
    re.compile(r"@Scheduled\s*\(([^)]{0,200})\)"),
    re.compile(r"@Cron\s*\(([^)]{0,200})\)"),
    re.compile(r"\bschedule\.every\s*\(([^)]{0,50})\)\s*\.\s*([\w.]{1,60})"),
    re.compile(r"\badd_job\s*\(([^)]{0,200})\)"),
)

_TELEMETRY_RES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"""\b(?:analytics|posthog|mixpanel|segment|amplitude|rudderanalytics|heap|plausible|umami|statsig|"""
        r"""telemetry|tracker|metrics|statsd)\s*\.\s*(?:track|capture|logEvent|log_event|record|emit|event|"""
        r"""increment|incr|count|counter|gauge|histogram|timing)\s*\(\s*(?:[A-Za-z_][\w.\[\]]*\s*,\s*)?"""
        r"""["'`]([^"'`]{2,120})["'`]""",
        re.I,
    ),
    re.compile(r"""\bgtag\s*\(\s*["']event["']\s*,\s*["']([^"']{2,120})["']"""),
    re.compile(
        r"""\b(?:track_event|trackEvent|capture_event|captureEvent|emit_event|emitEvent)\s*\(\s*"""
        r"""["'`]([^"'`]{2,120})["'`]"""
    ),
)

_SUBPROCESS_RES: tuple[tuple[re.Pattern[str], frozenset[str]], ...] = (
    (
        re.compile(r"""\bsubprocess\.(?:run|call|check_call|check_output|Popen)\s*\(\s*\[?\s*(?:f?["']([^"'\s]+))?"""),
        frozenset({".py"}),
    ),
    (re.compile(r"""\bos\.(?:system|popen|exec\w*|spawn\w*)\s*\(\s*(?:f?["']([^"'\s]+))?"""), frozenset({".py"})),
    (re.compile(r"""\bcreate_subprocess_(?:exec|shell)\s*\(\s*(?:f?["']([^"'\s]+))?"""), frozenset({".py"})),
    (
        re.compile(
            r"""(?:\bchild_process\.(?:exec|execSync|execFile|execFileSync|spawn|spawnSync)|"""
            r"""\b(?:execSync|execFileSync|spawnSync|execFile|spawn|execa|execaSync|execaCommand))"""
            r"""\s*\(\s*(?:["'`]([^"'`\s]+))?"""
        ),
        frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}),
    ),
    (re.compile(r"""\bexec\.Command(?:Context)?\s*\(\s*(?:ctx\s*,\s*)?(?:"([^"\s]+))?"""), frozenset({".go"})),
    (re.compile(r"""\bCommand::new\s*\(\s*(?:"([^"\s]+))?"""), frozenset({".rs"})),
    (
        re.compile(r"""\b(?:Runtime\.getRuntime\(\)\.exec|new\s+ProcessBuilder)\s*\(\s*(?:"([^"\s]+))?"""),
        frozenset({".java", ".kt"}),
    ),
)

_FS_PATH = re.compile(
    r"""["'`]((?:/(?:etc|var|usr|opt|srv|mnt|root|home|Library|Users|private|run|proc|sys|dev)/|~/)[^"'`\s]{0,200})["'`]"""
)
_FS_BENIGN = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/stdin", "/dev/tty", "/usr/bin/env", "/proc/self")

# Data flows (item 34): a personal or secret field on the same call as a sink.
_DATA_SINKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "log",
        re.compile(
            r"\b(?:(?:logger|logging|log|_log|_logger|LOG|LOGGER|console|slog|Log|Logger|winston|pino|bunyan)"
            r"\s*\.\s*(?:debug|info|warn|warning|error|exception|critical|fatal|trace|log|verbose|Info|Infof|"
            r"Error|Errorf|Debug|Debugf|Warn|Warnf|Print|Printf|Println|Fatal|Fatalf)"
            r"|print|println!|eprintln!|fmt\.(?:Print|Printf|Println|Fprint|Fprintf|Fprintln)"
            r"|System\.(?:out|err)\.print(?:ln|f)?|puts|(?:log::)?(?:info|warn|error|debug|trace)!)\s*\("
        ),
    ),
    (
        "telemetry",
        re.compile(
            r"\b(?:analytics|posthog|mixpanel|segment|amplitude|heap|Sentry|sentry_sdk|sentry|datadog|statsd|"
            r"newrelic|honeycomb|span|tracer|otel|telemetry|bugsnag|rollbar|LogRocket|fullstory|hotjar)"
            r"\s*\.\s*[A-Za-z_]\w*\s*\(|\b(?:set_user|set_tag|set_context|set_extra|set_attribute|setUser|setTag|"
            r"setContext|setExtra|setAttribute|setAttributes|gtag)\s*\("
        ),
    ),
    (
        "third_party",
        re.compile(
            r"\b(?:stripe|twilio|sendgrid|mailgun|mailchimp|postmark|resend|hubspot|salesforce|zendesk|openai|"
            r"anthropic|cohere|slack_sdk|boto3|s3|sqs|sns|ses|firebase|firestore|algolia|braze|customerio|"
            r"klaviyo|plaid|intercom)\s*\.\s*[\w.]+\s*\("
        ),
    ),
    (
        "external_endpoint",
        re.compile(
            r"\b(?:requests|httpx|aiohttp|session|client|axios|got|superagent|ky|needle|http|urllib3|"
            r"urllib\.request)\s*\.\s*(?:get|post|put|patch|delete|request|urlopen|Post|PostForm|Get|NewRequest|Do)"
            r"\s*\(|\bfetch\s*\("
        ),
    ),
)
_SECRET_TERMS = frozenset(
    {
        "password",
        "passwd",
        "passphrase",
        "secret",
        "client_secret",
        "api_key",
        "apikey",
        "access_key",
        "secret_key",
        "private_key",
        "access_token",
        "refresh_token",
        "auth_token",
        "id_token",
        "bearer_token",
        "session_token",
        "token",
        "credential",
        "credentials",
        "session_id",
        "sessionid",
        "cookie",
        "otp",
        "totp",
        "mfa_code",
        "signing_key",
        "ssh_key",
        "jwt",
    }
)
_PERSONAL_TERMS = frozenset(
    {
        "email",
        "email_address",
        "phone",
        "phone_number",
        "mobile_number",
        "ssn",
        "social_security",
        "social_security_number",
        "date_of_birth",
        "dob",
        "birthdate",
        "birthday",
        "street_address",
        "home_address",
        "postal_code",
        "zip_code",
        "zipcode",
        "passport",
        "passport_number",
        "national_id",
        "tax_id",
        "credit_card",
        "card_number",
        "cc_number",
        "cvv",
        "iban",
        "account_number",
        "ip_address",
        "first_name",
        "last_name",
        "full_name",
        "surname",
        "geolocation",
        "medical_record",
        "diagnosis",
    }
)
# ``token`` next to these words is a quantity or a kind, not a credential.
_TOKEN_NEIGHBOURS = frozenset(
    {"count", "counts", "limit", "limits", "usage", "budget", "len", "length", "type", "kind", "index", "idx"}
    | {"estimate", "estimator", "cost", "max", "min", "total", "stream", "ids", "position", "pos", "text"}
)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_STRING_LITERAL = re.compile(r"""(["'`])(?:\\.|(?!\1).)*\1""")
_INTERPOLATION = re.compile(r"\{([^{}]{1,200})\}")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _basename(path: str) -> str:
    return PurePosixPath(path).name


def _bounded_name(value: str) -> str:
    text = " ".join(str(value).split())
    return text[:_INV_MAX_NAME]


def _content_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def external_host(host: str) -> bool:
    """Whether *host* names a destination outside this system (not local, private or reserved)."""

    name = host.strip().lower().rstrip(".")
    if "." not in name or name in _NON_EXTERNAL_HOSTS:
        return False
    if name.endswith(_NON_EXTERNAL_SUFFIXES):
        return False
    match = _IPV4.match(name)
    if match:
        first, second = int(match.group(1)), int(match.group(2))
        return not (
            first in {0, 10, 127}
            or (first == 169 and second == 254)
            or (first == 172 and 16 <= second <= 31)
            or (first == 192 and second == 168)
        )
    return True


def _sensitive_terms(text: str) -> list[tuple[str, str]]:
    """``(class, term)`` for personal/secret identifiers in a call's argument text.

    String literal contents are ignored (a message mentioning "password" is not
    the password) except interpolated expressions (``f"{user.email}"``).
    """

    def interpolations(match: re.Match[str]) -> str:
        return " " + " ".join(_INTERPOLATION.findall(match.group(0))) + " "

    stripped = _STRING_LITERAL.sub(interpolations, text)
    found: dict[str, str] = {}
    for identifier in _IDENTIFIER.findall(stripped):
        words = [word for word in _CAMEL.sub("_", identifier).lower().split("_") if word]
        used: set[int] = set()
        for size in (3, 2, 1):
            for start in range(len(words) - size + 1):
                span = set(range(start, start + size))
                if span & used:
                    continue
                term = "_".join(words[start : start + size])
                if term == "token":
                    neighbours = {words[index] for index in (start - 1, start + 1) if 0 <= index < len(words)}
                    if neighbours & _TOKEN_NEIGHBOURS:
                        continue
                if term in _SECRET_TERMS:
                    found.setdefault(term, "secret")
                    used |= span
                elif term in _PERSONAL_TERMS:
                    found.setdefault(term, "personal")
                    used |= span
    return [(cls, term) for term, cls in sorted(found.items())]


def _call_text(lines: list[str], index: int, start: int) -> str:
    """The argument text of a call opened at ``lines[index][start]`` (up to four lines)."""

    depth = 0
    out: list[str] = []
    for row in range(index, min(len(lines), index + 4)):
        segment = lines[row][start:] if row == index else lines[row]
        for char in segment:
            out.append(char)
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth <= 0:
                    return "".join(out)[:400]
        out.append(" ")
    return "".join(out)[:400]


def _route_name(method: str, route: str) -> str:
    verb = method.upper() if method else "ANY"
    if verb in _ANY_METHODS:
        verb = "ANY"
    path = route if route.startswith("/") else "/" + route
    return f"{verb} {path}"


def _yaml_documents(text: str) -> list[Any]:
    try:
        return [doc for doc in yaml.safe_load_all(text) if doc is not None]
    except yaml.YAMLError:
        return []


def _workflow_facts(path: str, text: str) -> list[SideEffectFact]:
    """GitHub Actions token permissions, privileged triggers and schedules."""

    out: list[SideEffectFact] = []
    for doc in _yaml_documents(text)[:1]:
        if not isinstance(doc, Mapping):
            continue

        def grant(raw: object, scope: str) -> None:
            if isinstance(raw, str):
                out.append(SideEffectFact("permission", f"github-actions:{path}#{scope}:*", path, 0, value=raw))
            elif isinstance(raw, Mapping):
                for key, level in list(raw.items())[:32]:
                    out.append(
                        SideEffectFact("permission", f"github-actions:{path}#{scope}:{key}", path, 0, value=str(level))
                    )

        if "permissions" in doc:
            grant(doc.get("permissions"), "workflow")
        jobs = doc.get("jobs")
        if isinstance(jobs, Mapping):
            for job_id, job in list(jobs.items())[:64]:
                if isinstance(job, Mapping) and "permissions" in job:
                    grant(job.get("permissions"), str(job_id))
        # PyYAML reads the bare key ``on`` as boolean True.
        trigger = doc.get("on", doc.get(True))
        events = (
            trigger
            if isinstance(trigger, Mapping)
            else {str(item): None for item in trigger} if isinstance(trigger, list) else {str(trigger): None}
        )
        for privileged in ("pull_request_target", "workflow_run"):
            if privileged in events:
                out.append(SideEffectFact("permission", f"github-actions:{path}#trigger:{privileged}", path, 0))
        schedule = events.get("schedule") if isinstance(events, Mapping) else None
        if isinstance(schedule, list):
            for row in schedule[:16]:
                if isinstance(row, Mapping) and row.get("cron"):
                    out.append(
                        SideEffectFact("scheduled_job", f"{path}#cron:{_bounded_name(str(row['cron']))}", path, 0)
                    )
    return out


def _kubernetes_facts(path: str, text: str) -> list[SideEffectFact]:
    out: list[SideEffectFact] = []
    for doc in _yaml_documents(text)[:64]:
        if not isinstance(doc, Mapping):
            continue
        kind = str(doc.get("kind") or "")
        metadata = doc.get("metadata")
        name = str(metadata.get("name") or "") if isinstance(metadata, Mapping) else ""
        if kind == "CronJob":
            spec = doc.get("spec")
            schedule = str(spec.get("schedule") or "") if isinstance(spec, Mapping) else ""
            out.append(SideEffectFact("scheduled_job", f"k8s:{path}#{name}", path, 0, value=schedule))
        elif kind in {"Role", "ClusterRole"}:
            rules = doc.get("rules")
            for rule in rules[:32] if isinstance(rules, list) else ():
                if not isinstance(rule, Mapping):
                    continue
                resources = rule.get("resources") or ["*"]
                verbs = rule.get("verbs") or []
                for resource in list(resources)[:16] if isinstance(resources, list) else ():
                    for verb in list(verbs)[:16] if isinstance(verbs, list) else ():
                        out.append(SideEffectFact("permission", f"k8s-rbac:{kind}/{name}:{resource}:{verb}", path, 0))
    return out


def _json_document(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def _structured_facts(path: str, text: str) -> list[SideEffectFact]:
    name = _basename(path)
    suffix = PurePosixPath(path).suffix.lower()
    out: list[SideEffectFact] = []
    if re.match(r"^\.github/workflows/[^/]+\.ya?ml$", path):
        out.extend(_workflow_facts(path, text))
    elif suffix in {".yaml", ".yml"} and "kind:" in text and ("CronJob" in text or "Role" in text):
        out.extend(_kubernetes_facts(path, text))
    if name == "manifest.json" and '"manifest_version"' in text:
        doc = _json_document(text)
        if isinstance(doc, Mapping):
            for key in ("permissions", "host_permissions", "optional_permissions"):
                values = doc.get(key)
                for value in values[:64] if isinstance(values, list) else ():
                    out.append(SideEffectFact("permission", f"extension:{_bounded_name(str(value))}", path, 0))
    if name == "vercel.json" and "crons" in text:
        doc = _json_document(text)
        crons = doc.get("crons") if isinstance(doc, Mapping) else None
        for row in crons[:32] if isinstance(crons, list) else ():
            if isinstance(row, Mapping):
                job = f"{path}#{row.get('path') or ''}"
                out.append(SideEffectFact("scheduled_job", job, path, 0, value=str(row.get("schedule") or "")))
    if name == "crontab" or suffix == ".cron" or "/cron.d/" in f"/{path}":
        for index, line in enumerate(text.splitlines()[:500], start=1):
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and len(stripped.split()) >= 6:
                out.append(SideEffectFact("scheduled_job", f"{path}#{_bounded_name(stripped)}", path, index))
    return out


def _path_facts(path: str, text: str) -> list[SideEffectFact]:
    out: list[SideEffectFact] = []
    name = _basename(path)
    if _MIGRATION_PATH.search(path) and name != "__init__.py":
        out.append(SideEffectFact("migration", path, path, 0, value=_content_digest(text)))
    for pattern in _FILE_ROUTES:
        match = pattern.search(path)
        if match:
            out.append(SideEffectFact("route", _route_name("", match.group(1)), path, 0, detail="file-based route"))
            break
    return out


def side_effect_relevant(path: str) -> bool:
    """Whether :func:`inventory_side_effects` can state anything about *path* at all.

    A path-only check, so a register scan never reads a file that cannot
    contribute (images, documentation, archives).
    """

    suffix = PurePosixPath(path).suffix.lower()
    name = _basename(path)
    return (
        suffix in _CODE_SUFFIXES
        or suffix in _CONFIG_SUFFIXES
        or suffix in {".sql", ".prisma", ".cron"}
        or name in _CODE_BASENAMES
        or name.startswith("Dockerfile")
        or name == ".env"
        or name.startswith(".env.")
        or name.endswith(".env")
        or name == "crontab"
        or "/cron.d/" in f"/{path}"
        or bool(_MIGRATION_PATH.search(path))
        or any(pattern.search(path) for pattern in _FILE_ROUTES)
    )


def inventory_side_effects(path: str, text: str) -> tuple[SideEffectFact, ...]:
    """Every side-effect fact *text* (the exact content of *path*) states.

    Pure and deterministic. Which paths are in scope (tests, documentation and
    vendored files state no system side effect) is the caller's decision. A
    file over 1 MiB yields nothing; the caller reports that as degraded.
    """

    if len(text) > _INV_MAX_BYTES:
        return ()
    suffix = PurePosixPath(path).suffix.lower()
    name = _basename(path)
    is_code = suffix in _CODE_SUFFIXES or name in _CODE_BASENAMES or name.startswith("Dockerfile")
    is_config = suffix in _CONFIG_SUFFIXES
    is_sql = suffix == ".sql"
    is_env_file = name == ".env" or name.startswith(".env.") or name.endswith(".env")
    facts: list[SideEffectFact] = [*_path_facts(path, text), *_structured_facts(path, text)]
    if not (is_code or is_config or is_sql or is_env_file or suffix == ".prisma"):
        return _dedupe(facts)

    urls_allowed = name not in _METADATA_BASENAMES
    lines = text.splitlines()
    in_iam_list = False

    def add(category: str, value_name: str, line: int, *, value: str = "", detail: str = "") -> None:
        bounded = _bounded_name(value_name)
        if bounded:
            facts.append(SideEffectFact(category, bounded, path, line, value=value, detail=detail))

    for index, line in enumerate(lines):
        number = index + 1
        if len(line) > _INV_MAX_LINE or not line.strip() or _COMMENT_LINE.match(line):
            continue
        low = line.lower()
        if is_env_file:
            match = _ENV_FILE_LINE.match(line)
            if match:
                add("env_var", match.group(1), number, detail="declared in an environment file")
            continue
        if name.startswith("Dockerfile") or suffix == ".dockerfile":
            match = _DOCKER_ENV.match(line)
            if match:
                add("env_var", match.group(1), number, detail="Dockerfile ENV/ARG")
        if suffix == ".prisma":
            match = _PRISMA_MODEL.match(line)
            if match:
                add("db_table", match.group(1).lower(), number, detail="Prisma model")
            continue
        if "env" in low:
            for pattern in _ENV_RES:
                for found in pattern.finditer(line):
                    add("env_var", found.group(1), number)
        if ("settings" in low or "config" in low or "viper" in low or "@value" in low) and is_code:
            for pattern in _CONFIG_RES:
                for found in pattern.finditer(line):
                    add("config_key", found.group(1), number)
        if "://" in line and urls_allowed and not any(marker in low for marker in _URL_METADATA_MARKERS):
            for found in _URL_RE.finditer(line):
                host = found.group(1).lower().rstrip(".")
                if external_host(host):
                    add("outbound_host", host, number)
        if is_code:
            for pattern, method_group, path_group, suffixes in _ROUTE_RES:
                if suffix not in suffixes:
                    continue
                for found in pattern.finditer(line):
                    method = found.group(method_group) if method_group else ""
                    add("route", _route_name(method, found.group(path_group)), number)
            if name == "urls.py" and "path(" in low:
                for found in _DJANGO_PATH.finditer(line):
                    add("route", _route_name("", found.group(1)), number)
            if _CLI_CALL.search(line):
                for found in _CLI_DASHED.finditer(line):
                    add("cli_flag", "--" + found.group(1), number)
            for pattern, suffixes in _CLI_NAMED:
                if suffix in suffixes:
                    for found in pattern.finditer(line):
                        add("cli_flag", "--" + found.group(1), number)
        if "table" in low or "truncate" in low or "model" in low or "drop" in low or "remove" in low:
            for op, pattern in _SQL_FILE_RES if is_sql else _SQL_CODE_RES:
                for found in pattern.finditer(line):
                    table = found.group(1).lower()
                    if op == "create":
                        add("db_table", table, number, detail="CREATE TABLE")
                    elif op == "drop_column":
                        add("db_drop", f"{path}::drop_column:{table}.{found.group(2).lower()}", number)
                    else:
                        add("db_drop", f"{path}::{op}:{table}", number)
            if is_code:
                for pattern in _ORM_TABLES:
                    for found in pattern.finditer(line):
                        add("db_table", found.group(1).lower(), number)
                for op, pattern in _ORM_DROPS:
                    for found in pattern.finditer(line):
                        target = found.group(1).lower()
                        if op == "drop_column":
                            target = f"{target}.{found.group(2).lower()}"
                        add("db_drop", f"{path}::{op}:{target}", number)
        if is_config or is_code:
            if _IAM_KEY.search(line) or in_iam_list:
                for found in _IAM_TOKEN.finditer(line):
                    if not found.group(1).startswith(("http", "https")):
                        add("permission", f"iam:{found.group(1)}", number)
                if "[" in line and "]" not in line:
                    in_iam_list = bool(_IAM_KEY.search(line)) or in_iam_list
                elif "]" in line:
                    in_iam_list = False
            if "scope" in low and "fixture" not in low:
                scope_match = _SCOPE_LINE.search(line)
                if scope_match:
                    for quoted in _QUOTED.findall(scope_match.group(1)):
                        for token in re.split(r"[\s,]+", quoted):
                            if (
                                token in _KNOWN_SCOPES
                                or ":" in token
                                or "/auth/" in token
                                or (re.fullmatch(r"[A-Za-z]+\.[A-Za-z.]+", token) is not None)
                            ):
                                add("permission", f"oauth:{token}", number)
        if name == "AndroidManifest.xml" and "uses-permission" in low:
            for found in _ANDROID_PERMISSION.finditer(line):
                add("permission", f"android:{found.group(1)}", number)
        if not is_code:
            continue
        if any(token in low for token in ("variation", "enabled", "ison", "is_on", "flag", "treatment", "gate")):
            for pattern in _FLAG_RES:
                for found in pattern.finditer(line):
                    add("feature_flag", found.group(1), number)
        if any(token in low for token in ("cron", "schedule", "periodic_task", "add_job")):
            for pattern in _SCHEDULE_RES:
                for found in pattern.finditer(line):
                    spec = " ".join(part for part in found.groups() if part)
                    add("scheduled_job", f"{path}#{_bounded_name(spec) or 'schedule'}", number)
        if any(
            token in low
            for token in ("track", "capture", "logevent", "log_event", "record", "emit", "incr", "gauge", "gtag")
        ) or ("count" in low or "timing" in low or "histogram" in low):
            for pattern in _TELEMETRY_RES:
                for found in pattern.finditer(line):
                    add("telemetry_event", found.group(1), number)
        if any(token in low for token in ("subprocess", "os.", "spawn", "exec", "command::new", "processbuilder")):
            for pattern, suffixes in _SUBPROCESS_RES:
                if suffix not in suffixes:
                    continue
                for found in pattern.finditer(line):
                    program = found.group(1)
                    add(
                        "subprocess",
                        PurePosixPath(program).name if program else f"(dynamic)@{path}",
                        number,
                        detail="" if program else "command built at runtime",
                    )
        if "/" in line:
            for found in _FS_PATH.finditer(line):
                target = found.group(1)
                if not target.startswith(_FS_BENIGN):
                    add("filesystem_path", target, number)
        for sink, pattern in _DATA_SINKS:
            for found in pattern.finditer(line):
                for cls, term in _sensitive_terms(_call_text(lines, index, found.end() - 1)):
                    add(
                        "data_flow",
                        f"{path}::{sink}:{cls}:{term}",
                        number,
                        detail=f"{cls} field '{term}' reaches {sink.replace('_', ' ')}",
                    )
    return _dedupe(facts)


def _dedupe(facts: list[SideEffectFact]) -> tuple[SideEffectFact, ...]:
    seen: set[tuple[str, str, str]] = set()
    out: list[SideEffectFact] = []
    for fact in facts:
        key = (fact.category, fact.name, fact.value)
        if key in seen:
            continue
        seen.add(key)
        out.append(fact)
    return tuple(out)


__all__ = [
    "ABSENT",
    "CONTRACT_VERSION",
    "SIDE_EFFECT_CATEGORIES",
    "SIDE_EFFECT_INVENTORY_VERSION",
    "UNANALYZED_PREFIX",
    "UNAVAILABLE",
    "SideEffectFact",
    "contract_fingerprint",
    "external_host",
    "inventory_side_effects",
    "side_effect_relevant",
    "split_reference",
]
