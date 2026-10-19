"""G8-DIAG: the shared diagnostic bundle and ``lc review diagnostics`` (local surface).

Every canary below is a synthetic string generated here. Secret canaries (API
key, GitHub token, bearer token, an environment value, URL credentials and a
query secret, a provider key in ``providers.json``) and source canaries (file
content, a diff hunk, a prompt, model output, a comment body) are planted into
every column of every Review table the collector touches, the degradation
reasons, the trusted-base config, the repository and the server's discovery
document. No build of the bundle -- default or any include -- may carry one.
The hosted surface has its own canary test under ``enterprise/server/tests``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import subprocess
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli import cli
from lemoncrow.gateway.cli.commands.review_diagnostics_cli import _INCLUDE_CHOICES
from lemoncrow.pro.capabilities.review import diagnostic_local
from lemoncrow.pro.capabilities.review.diagnostic_bundle import (
    BUNDLE_SCHEMA,
    BUNDLE_SCHEMA_VERSION,
    HOSTED_INCLUDES,
    INCLUDE_OPTIONS,
    LOCAL_INCLUDES,
    MAX_INCLUDED_TEXT_CHARS,
    MAX_REASON_CHARS,
    MAX_TEXT_CHARS,
    MAX_TEXT_INPUT_CHARS,
    PLACEHOLDER,
    RECORD_FIELDS,
    SECTIONS,
    UNAVAILABLE_REASONS,
    WITHHELD,
    DataFlowFacts,
    DiagnosticFacts,
    DiagnosticLimits,
    IntelligenceJobRecord,
    ProtocolFacts,
    ProviderOperationRecord,
    QueueJobRecord,
    RecordSource,
    SessionRow,
    SnapshotRecord,
    VersionFacts,
    WebhookDeliveryRecord,
    build_diagnostic_bundle,
    collect_review_records,
    redact_text,
    require_includes,
    scrub_included_text,
)
from lemoncrow.pro.capabilities.review.diagnostic_local import local_diagnostic_facts
from lemoncrow.pro.capabilities.review.store import ReviewStore

# --------------------------------------------------------------------------- #
# canaries (synthetic)
# --------------------------------------------------------------------------- #

API_KEY_BODY = "Q7vXk2mN9pLr4TzW8yBc3DfGh6JkLmNp"
API_KEY = f"sk-proj-{API_KEY_BODY}"
GH_TOKEN_BODY = "Zx8Wc7Vb6Nm5Lk4Jh3Gf2Ds1Aq0PoIuYtR9e"
GH_TOKEN = f"ghp_{GH_TOKEN_BODY}"
BEARER_BODY = "bT9xQ2wE7rY4uI1oP6aS3dF8gH5jK0lZ"
BEARER = f"Authorization: Bearer {BEARER_BODY}"
ENV_NAME = "G8DIAG_PLANTED_SECRET"
#: Separator-broken so only the environment-value rule can catch it.
ENV_VALUE = "plum-violet-Q9-heron-K2-amber"
URL_PASSWORD = "s3cr3t-Pw-hX7"
URL_QUERY = "qs-tK9-vL2"
URL = f"https://alice:{URL_PASSWORD}@git.example.invalid/acme.git?access_token={URL_QUERY}"
PROVIDER_KEY_BODY = "Pv8Kd3Mx7Qa2Wn5Ze9Rb4Tc6Yf1Hg0Lj"
PROVIDER_KEY = f"sk-{PROVIDER_KEY_BODY}"
PROVIDER_URL_PASSWORD = "pr0v-Pw-mK4"
#: Endpoint names land in data-flow origins (``providers.json endpoints.<name>``): one credential-shaped
#: with no 16+ run (only the credential check catches it), one opaque run (only the run check catches it).
SETTING_KEY_BODY = "Wd3T-r8Kq-2Lm9-Xb6Z-4Pn7-Hc1V"
SETTING_KEY = f"sk-proj-{SETTING_KEY_BODY}"
SETTING_RUN = "Jx5Mq8Vt2Rk9Lb4Nw7Cz"
#: A full-width spelling of an API key, and its NFKC (ASCII) body.
FULLWIDTH_ASCII_BODY = "Z8kL2mQ9vR4tX7wB3n"


def _fullwidth(text: str) -> str:
    return "".join(chr(ord(char) + 0xFEE0) for char in text)


FULLWIDTH_KEY_BODY = _fullwidth(FULLWIDTH_ASCII_BODY)
FULLWIDTH_KEY = _fullwidth("sk-proj-") + FULLWIDTH_KEY_BODY
SECRET_MARKERS = (
    API_KEY_BODY,
    GH_TOKEN_BODY,
    BEARER_BODY,
    ENV_VALUE,
    URL_PASSWORD,
    URL_QUERY,
    PROVIDER_KEY_BODY,
    PROVIDER_URL_PASSWORD,
    SETTING_KEY_BODY,
    SETTING_RUN,
    FULLWIDTH_KEY_BODY,
    FULLWIDTH_ASCII_BODY,
)

FILE_CONTENT = "def leaked_function_Fq7Rw2():\n    return 'Hx4Kp9'\n"
HUNK = "@@ -1,2 +1,3 @@\n-old_line_Vn3Bm8\n+new_line_Jt5Gc1\n"
PROMPT = "You are a reviewer. PROMPT_Yd6Ue2 Review this diff."
MODEL_OUTPUT = "The change looks risky OUTPUT_Wk8Ls3"
COMMENT = "Please rename this COMMENT_Pz4Nx7"
#: Non-ASCII source text: Cyrillic, full-width letters, Arabic-Indic digits.
NON_ASCII_MARKERS = (
    "\u0441\u0435\u043a\u0440\u0435\u0442\u043d\u044b\u0439_\u043a\u043b\u044e\u0447",  # Cyrillic
    "\u0416\u0431\u0440\u0432\u0433",  # Cyrillic
    _fullwidth("SECRETxyz"),
    "\u0663\u0664\u0665\u0666\u0667\u0668\u0669\u0660",  # Arabic-Indic digits
)
NON_ASCII_SOURCE = " ".join(NON_ASCII_MARKERS)
SOURCE_MARKERS = (
    "leaked_function_Fq7Rw2",
    "Hx4Kp9",
    "old_line_Vn3Bm8",
    "new_line_Jt5Gc1",
    "PROMPT_Yd6Ue2",
    "OUTPUT_Wk8Ls3",
    "COMMENT_Pz4Nx7",
    *NON_ASCII_MARKERS,
)
CANARY_MARKERS = SECRET_MARKERS + SOURCE_MARKERS

#: Rides every error column; only ``--include error-text`` may show it.
ERROR_MARK = "errmark_Hb2Qe5"
ERROR_TEXT = (
    f"provider request failed {ERROR_MARK} key={API_KEY} {BEARER} gh={GH_TOKEN} env={ENV_VALUE} "
    f"remote={URL} provider_key={PROVIDER_KEY} fw={FULLWIDTH_KEY}"
)
SECRETS_TEXT = f"{API_KEY} {BEARER} {GH_TOKEN} {ENV_VALUE} {URL} {PROVIDER_KEY} {FULLWIDTH_KEY}"
CONTENT = "\n".join((FILE_CONTENT, HUNK, PROMPT, MODEL_OUTPUT, COMMENT, NON_ASCII_SOURCE, SECRETS_TEXT))
JSON_CONTENT = json.dumps(
    {
        "prompt": PROMPT,
        "output": MODEL_OUTPUT,
        "hunk": HUNK,
        "file": FILE_CONTENT,
        "comment": COMMENT,
        "non_ascii": NON_ASCII_SOURCE,
        "secrets": SECRETS_TEXT,
    },
    ensure_ascii=False,
)
DEGRADED_PLAIN = "packet degraded: diff truncated"
DEGRADED = [DEGRADED_PLAIN, f"diff unreadable near {HUNK} {FILE_CONTENT} {NON_ASCII_SOURCE} {SECRETS_TEXT}"]

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
SALT = b"g8-diag-test-salt"


def _dump(document: object) -> str:
    """JSON with non-ASCII kept literal, so a non-ASCII canary cannot hide behind ``\\u`` escapes."""

    return json.dumps(document, ensure_ascii=False)


def _assert_clean(text: str, *, allow: Sequence[str] = ()) -> None:
    lowered = text.lower()
    for marker in CANARY_MARKERS:
        if marker in allow:
            continue
        assert marker.lower() not in lowered, f"canary {marker[:6]}... leaked"


def _at(minutes: int) -> str:
    return (NOW - timedelta(minutes=minutes)).isoformat()


# --------------------------------------------------------------------------- #
# a real local Review partition with canaries in every column
# --------------------------------------------------------------------------- #


def _insert(conn: sqlite3.Connection, table: str, values: Mapping[str, Any]) -> None:
    """Insert one row: *values* for structural columns, a canary in every other TEXT column."""

    row: dict[str, Any] = {}
    for _cid, name, kind, *_rest in conn.execute(f"PRAGMA table_info({table})").fetchall():
        if name in values:
            row[name] = values[name]
        elif str(kind).upper() == "TEXT":
            row[name] = JSON_CONTENT if name.endswith("_json") else CONTENT
    placeholders = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({placeholders})", tuple(row.values()))


def _partition(review_root: Path, repo_id: str) -> Path:
    root = review_root / repo_id
    ReviewStore(root).list_sessions(status="", limit=1)  # create and migrate the store
    return root / "lemoncrow_reviews.db"


def _plant_review(
    db: Path,
    review_id: str,
    *,
    minutes: int,
    runs: int = 2,
    syntheses: int = 1,
    deliveries: int = 1,
    degraded: Sequence[str] = tuple(DEGRADED),
) -> dict[str, Any]:
    """One Review with a Snapshot, failed AI jobs, an annotation and failed feedback deliveries."""

    snapshot_id = f"{review_id}-s1"
    ids: dict[str, Any] = {"review_id": review_id, "snapshot_id": snapshot_id, "runs": [], "operations": []}
    conn = sqlite3.connect(db)
    try:
        _insert(
            conn,
            "review_sessions",
            {
                "id": review_id,
                "status": "open",
                "current_revision_id": snapshot_id,
                "created_at": _at(minutes + 5),
                "updated_at": _at(minutes),
            },
        )
        _insert(
            conn,
            "review_revisions",
            {
                "id": snapshot_id,
                "review_id": review_id,
                "revision_number": 1,
                "packet_schema_version": 3,
                "capture_kind": "packet",
                "created_at": _at(minutes + 4),
                "degraded_json": json.dumps(list(degraded)),
            },
        )
        for index in range(runs):
            run_id = f"{review_id}-run{index}"
            ids["runs"].append(run_id)
            _insert(
                conn,
                "review_intelligence_runs",
                {
                    "review_id": review_id,
                    "id": run_id,
                    "revision_id": snapshot_id,
                    "capability": "brief",
                    "driver": "openai",
                    "status": "failed",
                    # One clean model id (shown by --include model-names), one credential-shaped.
                    "model": "gpt-4o-mini" if index % 2 == 0 else API_KEY,
                    "error": ERROR_TEXT,
                    "model_calls": 2,
                    "created_at": _at(minutes + 3 - index / 100),
                    "completed_at": _at(minutes + 2 - index / 100),
                },
            )
        for index in range(syntheses):
            synthesis_id = f"{review_id}-syn{index}"
            ids["runs"].append(synthesis_id)
            _insert(
                conn,
                "review_plan_syntheses",
                {
                    "review_id": review_id,
                    "id": synthesis_id,
                    "revision_id": snapshot_id,
                    "status": "failed",
                    "model": CONTENT,
                    "error": ERROR_TEXT,
                    "created_at": _at(minutes + 3),
                    "completed_at": _at(minutes + 2),
                },
            )
        annotation_id = f"{review_id}-ann"
        _insert(
            conn,
            "annotations",
            {
                "id": annotation_id,
                "review_id": review_id,
                "revision_id": snapshot_id,
                "created_at": _at(minutes + 2),
                "updated_at": _at(minutes + 2),
            },
        )
        for index in range(deliveries):
            operation_id = f"{review_id}-op{index}"
            ids["operations"].append(operation_id)
            _insert(
                conn,
                "deliveries",
                {
                    "id": f"{review_id}-dl{index}",
                    "annotation_id": annotation_id,
                    "target_type": "github",
                    "state": "failed",
                    "operation_id": operation_id,
                    "revision_id": snapshot_id,
                    "last_error": ERROR_TEXT,
                    "created_at": _at(minutes + 1),
                    "updated_at": _at(minutes + 1 - index / 100),
                },
            )
        conn.commit()
    finally:
        conn.close()
    return ids


def _git_repo(path: Path) -> Path:
    path.mkdir()
    git = ["git", "-C", str(path), "-c", "user.email=t@example.invalid", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "app.py").write_text(FILE_CONTENT, encoding="utf-8")
    (path / ".lemoncrow").mkdir()
    (path / ".lemoncrow" / "review.yaml").write_text(
        f"# {FILE_CONTENT.splitlines()[0]} {API_KEY} {URL}\nreview:\n  capabilities: {{ai: off}}\n", encoding="utf-8"
    )
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "-c", "commit.gpgsign=false", "commit", "-qm", "init"], check=True)
    # An uncommitted change: the diff hunk canary lives in the working tree.
    (path / "app.py").write_text(FILE_CONTENT + HUNK, encoding="utf-8")
    return path


def _discovery_document(_config: object) -> dict[str, Any]:
    from lemoncrow_client.protocol import protocol_window

    current, previous = protocol_window()
    return {
        "name": "lemoncrow-server",
        "protocol_version": current,
        "supported_protocol_versions": [current, previous, API_KEY],
        "deployment": {"mode": API_KEY.lower(), "url": URL},
        "url": URL,
        "token": GH_TOKEN,
        "product_capabilities": {
            "review": {
                "manifest": 1,
                "brief": True,
                "ask": True,
                "capture_modes": ["packet", API_KEY],
                "evidence_kinds": ["screenshot", FILE_CONTENT],
                "capture_options": ["title", ENV_VALUE],
                "schemas": {"packet": [1, API_KEY], "x": [PROMPT]},
                COMMENT: True,
            }
        },
    }


@pytest.fixture
def planted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A store root with providers.json, two Review partitions, a repository and env canaries."""

    root = tmp_path / "root"
    root.mkdir()
    endpoints = {
        "custom": {
            "endpoints": {
                "lab": {
                    "base_url": f"https://bob:{PROVIDER_URL_PASSWORD}@models.example.invalid/v1?key={URL_QUERY}",
                    "api_key": PROVIDER_KEY,
                    "models": {"qwen-coder": {}},
                },
                SETTING_KEY: {"base_url": "https://models.example.invalid/v1", "models": {"m": {}}},
                SETTING_RUN: {"base_url": "https://models.example.invalid/v1", "models": {"m": {}}},
            }
        }
    }
    (root / "providers.json").write_text(json.dumps(endpoints), encoding="utf-8")
    review_root = root / "server" / "data" / "reviews"
    alpha = _plant_review(_partition(review_root, "repo-alpha"), "rv-alpha-1", minutes=10)
    beta = _plant_review(_partition(review_root, "repo-beta"), "rv-beta-1", minutes=20)
    monkeypatch.delenv("LEMONCROW_SERVER_REVIEW_ROOT", raising=False)
    monkeypatch.setenv(ENV_NAME, ENV_VALUE)
    monkeypatch.setenv("HTTPS_PROXY", f"http://carol:{URL_PASSWORD}@proxy.invalid:3128")
    calls: list[object] = []

    def discover(config: object) -> dict[str, Any]:
        calls.append(config)
        return _discovery_document(config)

    monkeypatch.setattr(diagnostic_local, "discover_server", discover)
    return {
        "root": root,
        "review_root": review_root,
        "repo": _git_repo(tmp_path / "repo"),
        "reviews": (alpha, beta),
        "discovery_calls": calls,
    }


def _invoke(planted: Mapping[str, Any], *args: str) -> Any:
    return CliRunner().invoke(
        cli,
        ["--root", str(planted["root"]), "review", "diagnostics", "--repo", str(planted["repo"]), *args],
        catch_exceptions=False,
    )


def _bundle(planted: Mapping[str, Any], *args: str) -> tuple[str, dict[str, Any]]:
    result = _invoke(planted, *args)
    assert result.exit_code == 0, result.output[:400]
    return result.output, json.loads(result.output)


INCLUDE_SETS: tuple[tuple[str, ...], ...] = ((), *((name,) for name in LOCAL_INCLUDES), tuple(LOCAL_INCLUDES))


# --------------------------------------------------------------------------- #
# canary acceptance (local)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("include", INCLUDE_SETS, ids=lambda names: "+".join(names) or "default")
def test_local_bundle_carries_no_canary_and_the_expected_ids_and_states(
    planted: dict[str, Any], include: tuple[str, ...]
) -> None:
    args = [arg for name in include for arg in ("--include", name)]
    text, bundle = _bundle(planted, *args)
    _assert_clean(text)
    _assert_clean(_dump(bundle))
    assert (ERROR_MARK.lower() in text.lower()) == ("error-text" in include)
    assert planted["discovery_calls"], "the fake discovery document was never read"

    sections = bundle["sections"]
    alpha, beta = planted["reviews"]
    assert bundle["schema"] == BUNDLE_SCHEMA and bundle["schema_version"] == BUNDLE_SCHEMA_VERSION
    assert bundle["surface"] == "local" and set(sections) == set(SECTIONS)
    assert bundle["scope"]["repositories_scanned"] == 2 and bundle["scope"]["repositories_total"] == 2
    snapshots = sections["snapshots"]["records"]
    assert [(row["review_id"], row["snapshot_id"], row["repo_id"]) for row in snapshots] == [
        (alpha["review_id"], alpha["snapshot_id"], "repo-alpha"),
        (beta["review_id"], beta["snapshot_id"], "repo-beta"),
    ]
    assert {(row["status"], row["capture_kind"], row["snapshot_number"]) for row in snapshots} == {
        ("open", "packet", 1)
    }
    jobs = sections["intelligence_jobs"]["records"]
    assert {row["job_id"] for row in jobs} == set(alpha["runs"]) | set(beta["runs"])
    assert {(row["state"], row["has_error"]) for row in jobs} == {("failed", True)}
    assert {row["capability"] for row in jobs} == {"brief", "plan_synthesis"}
    operations = sections["provider_operations"]["records"]
    assert {row["operation_id"] for row in operations} == set(alpha["operations"]) | set(beta["operations"])
    assert {(row["provider"], row["state"]) for row in operations} == {("github", "failed")}
    degradations = sections["degradations"]
    assert redact_text(DEGRADED_PLAIN, limit=MAX_REASON_CHARS) in degradations["by_reason"]
    assert {row["snapshot_id"] for row in degradations["records"]} == {alpha["snapshot_id"], beta["snapshot_id"]}
    errors = sections["recent_errors"]["records"]
    assert {row["source"] for row in errors} == {"intelligence_job", "provider_operation"}
    assert {row["message"] for row in errors} == {redact_text(ERROR_TEXT)}
    assert len({row["fingerprint"] for row in errors}) == 1
    protocol = sections["protocol"]
    assert protocol["status"] == "populated" and protocol["compatible"] is True
    assert protocol["server"]["name"] == "lemoncrow-server"
    assert protocol["server"]["deployment_mode"] == "other"
    assert protocol["review_manifest"]["features"] == {"brief": True, "ask": True}
    assert protocol["review_manifest"]["capture_modes"] == ["packet"]
    flow = sections["data_flow"]
    assert flow["status"] == "populated" and flow["repositories"][0]["repository"] == "current_repository"
    rows = flow["repositories"][0]["rows"]
    origins = {origin["setting"] for row in rows for origin in row["origins"]}
    assert "HTTPS_PROXY" in origins and "lc --root" in origins
    # Both hostile endpoint names are withheld by the data-flow itself: a positional
    # row id and origin setting stand in for each.
    assert "providers.json endpoints.lab" in origins
    assert "models.endpoint.lab" in {row["id"] for row in rows}
    hostile = [row["id"].removeprefix("models.endpoint.") for row in rows if row["id"].startswith("models.endpoint.")]
    positions = [key for key in hostile if key != "lab"]
    assert len(positions) == 2 and all(key.isdigit() for key in positions)
    assert {f"providers.json endpoints.{key}" for key in positions} <= origins
    assert sections["queue_jobs"]["reason"] == "no_durable_queue"
    assert sections["webhook_deliveries"]["reason"] == "no_provider_webhooks"
    assert any(marker.startswith("session:") for marker in sections["versions"]["store_schemas"])


def test_the_local_collector_never_writes_the_review_store(planted: dict[str, Any]) -> None:
    dbs = sorted(planted["review_root"].glob("*/lemoncrow_reviews.db"))
    before = [hashlib.sha256(db.read_bytes()).hexdigest() for db in dbs]
    _bundle(planted)
    assert [hashlib.sha256(db.read_bytes()).hexdigest() for db in dbs] == before


def test_out_writes_an_owner_only_file(planted: dict[str, Any], tmp_path: Path) -> None:
    target = tmp_path / "bundle.json"
    result = _invoke(planted, "--out", str(target))
    assert result.exit_code == 0, result.output[:400]
    assert target.stat().st_mode & 0o777 == 0o600
    document = json.loads(target.read_text(encoding="utf-8"))
    assert set(document["sections"]) == set(SECTIONS)
    _assert_clean(target.read_text(encoding="utf-8"))
    _assert_clean(_dump(document))


# --------------------------------------------------------------------------- #
# includes
# --------------------------------------------------------------------------- #


def _without(bundle: Mapping[str, Any], section: str, key: str) -> dict[str, Any]:
    out = copy.deepcopy(dict(bundle))
    for record in out["sections"][section]["records"]:
        record.pop(key)
    out["include"] = {**out["include"], "requested": [], "adds": {}}
    return out


def _builder_facts(tmp_path: Path) -> DiagnosticFacts:
    review_root = tmp_path / "reviews"
    _plant_review(_partition(review_root, "repo-alpha"), "rv-alpha-1", minutes=10)
    return local_diagnostic_facts(
        review_root=review_root,
        protocol=ProtocolFacts(client_window=("2026-10-05", "2026-09-14"), server_reason="server_not_configured"),
        data_flow=DataFlowFacts(unavailable="not_a_repository"),
    )


def _build(facts: DiagnosticFacts, *include: str, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("environ", {ENV_NAME: ENV_VALUE})
    return build_diagnostic_bundle(
        facts, include=include, allowed_includes=LOCAL_INCLUDES, requested_by="local", now=NOW, salt=SALT, **kwargs
    )


def test_error_text_adds_exactly_the_error_text(tmp_path: Path) -> None:
    facts = _builder_facts(tmp_path)
    default, included = _build(facts), _build(facts, "error-text")
    assert tuple(default["sections"]) == tuple(included["sections"]) == SECTIONS
    records = included["sections"]["recent_errors"]["records"]
    assert records and all(ERROR_MARK in record["text"] for record in records)
    assert _without(included, "recent_errors", "text") == default
    assert included["include"] == {
        "requested": ["error-text"],
        "allowed": list(LOCAL_INCLUDES),
        "by": "local",
        "at": default["generated_at"],
        "adds": {"error-text": INCLUDE_OPTIONS["error-text"]},
    }
    _assert_clean(_dump(included))


def test_model_names_adds_exactly_the_model_names(tmp_path: Path) -> None:
    facts = _builder_facts(tmp_path)
    default, included = _build(facts), _build(facts, "model-names")
    models = {record["job_id"]: record["model"] for record in included["sections"]["intelligence_jobs"]["records"]}
    # The clean id is shown; a credential-shaped "model" is withheld; multi-line text is not a model id.
    assert models == {"rv-alpha-1-run0": "gpt-4o-mini", "rv-alpha-1-run1": WITHHELD, "rv-alpha-1-syn0": ""}
    assert _without(included, "intelligence_jobs", "model") == default
    assert included["include"]["requested"] == ["model-names"] and included["include"]["by"] == "local"
    _assert_clean(_dump(included))


def test_default_records_carry_only_allowlisted_fields(tmp_path: Path) -> None:
    expected = {
        "snapshots": "review_id repo_id status snapshot_id snapshot_number capture_kind packet_schema_version "
        "snapshot_created_at review_updated_at has_error",
        "intelligence_jobs": "job_id kind review_id repo_id snapshot_id capability driver state model_calls "
        "created_at completed_at has_error",
        "provider_operations": "operation_id review_id repo_id snapshot_id provider kind state attempts remote_ref "
        "created_at updated_at has_error",
    }
    bundle = _build(_builder_facts(tmp_path))
    for section, names in expected.items():
        assert {name for name, _kind in RECORD_FIELDS[section]} | {"has_error"} == set(names.split())
        for record in bundle["sections"][section]["records"]:
            assert set(record) == set(names.split()), section
    assert set(bundle["sections"]["recent_errors"]["records"][0]) == {
        "source",
        "ref",
        "repo_id",
        "at",
        "message",
        "fingerprint",
    }
    assert set(bundle["sections"]["degradations"]["records"][0]) == {
        "review_id",
        "repo_id",
        "snapshot_id",
        "reasons",
        "reasons_total",
    }


def test_unknown_includes_are_refused_by_the_builder_and_the_cli(tmp_path: Path, planted: dict[str, Any]) -> None:
    facts = _builder_facts(tmp_path)
    for bad in (["source"], ["error-text", "secrets"], ["Error-Text"], [""]):
        with pytest.raises(ValueError, match="unknown include"):
            _build(facts, *bad)
    for name in LOCAL_INCLUDES:
        with pytest.raises(ValueError, match="none on this surface"):
            build_diagnostic_bundle(facts, include=(name,), allowed_includes=(), requested_by="admin")
    assert require_includes(["model-names", "error-text", "model-names"], LOCAL_INCLUDES) == (
        "error-text",
        "model-names",
    )
    result = _invoke(planted, "--include", "source")
    assert result.exit_code == 2 and "error-text" in result.output and "model-names" in result.output


def test_cli_include_choices_are_the_builders_local_includes() -> None:
    assert _INCLUDE_CHOICES == LOCAL_INCLUDES == tuple(INCLUDE_OPTIONS)
    # Hosted names the same two, but only as privileged explicit includes (see the hosted tests).
    assert HOSTED_INCLUDES == LOCAL_INCLUDES


# --------------------------------------------------------------------------- #
# completeness: populated or explicitly unavailable
# --------------------------------------------------------------------------- #


def _assert_complete(bundle: Mapping[str, Any]) -> dict[str, str]:
    sections = bundle["sections"]
    assert set(sections) == set(SECTIONS)
    reasons: dict[str, str] = {}
    for name, section in sections.items():
        assert section["status"] in {"populated", "unavailable", "unknown"}, name
        if section["status"] in {"unavailable", "unknown"}:
            assert section["reason"] in UNAVAILABLE_REASONS, name
            assert section["detail"] == UNAVAILABLE_REASONS[section["reason"]]
            reasons[name] = section["reason"]
    return reasons


def test_every_section_is_explicitly_unavailable_on_an_empty_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    monkeypatch.delenv("LEMONCROW_SERVER_REVIEW_ROOT", raising=False)

    def unreachable(_config: object) -> dict[str, Any]:
        raise OSError(f"connect to {URL} failed")

    monkeypatch.setattr(diagnostic_local, "discover_server", unreachable)
    result = CliRunner().invoke(
        cli, ["--root", str(tmp_path / "root"), "review", "diagnostics"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output[:400]
    _assert_clean(result.output)
    _assert_clean(_dump(json.loads(result.output)))
    reasons = _assert_complete(json.loads(result.output))
    assert reasons == {
        "data_flow": "not_a_repository",
        "execution_locus": "not_a_repository",
        "snapshots": "no_review_store",
        "intelligence_jobs": "no_review_store",
        "queue_jobs": "no_durable_queue",
        "job_lifecycle": "job_lifecycle_not_reported",
        "background_budget": "background_budget_not_reported",
        "provider_operations": "no_review_store",
        "webhook_deliveries": "no_provider_webhooks",
        "degradations": "derived_unavailable",
        "recent_errors": "derived_unavailable",
    }
    protocol = json.loads(result.output)["sections"]["protocol"]
    assert protocol["server"] is None and protocol["notes"][0]["reason"] == "server_unreachable"


def test_an_unreadable_store_is_reported_not_omitted(tmp_path: Path) -> None:
    review_root = tmp_path / "reviews"
    (review_root / "repo-broken").mkdir(parents=True)
    (review_root / "repo-broken" / "lemoncrow_reviews.db").write_bytes(b"not a database" * 64)
    facts = local_diagnostic_facts(
        review_root=review_root,
        protocol=ProtocolFacts(server_reason="server_not_configured"),
        data_flow=DataFlowFacts(unavailable="data_flow_failed"),
    )
    bundle = _build(facts)
    reasons = _assert_complete(bundle)
    assert reasons["snapshots"] == reasons["intelligence_jobs"] == "store_unreadable"
    assert bundle["scope"]["repositories_unreadable"] == 1
    assert reasons["protocol"] == "server_not_configured"


def test_the_builder_refuses_an_unregistered_unavailable_reason() -> None:
    facts = DiagnosticFacts(
        surface="local",
        versions=VersionFacts(),
        protocol=ProtocolFacts(server_reason="server_not_configured"),
        data_flow=DataFlowFacts(unavailable=f"free text {API_KEY}"),
        records=collect_review_records([], webhooks=False, unavailable="no_review_store"),
        queue_jobs=RecordSource(unavailable="no_durable_queue"),
    )
    with pytest.raises(ValueError, match="unregistered unavailable reason"):
        _build(facts)


# --------------------------------------------------------------------------- #
# size bounds: at and past every limit
# --------------------------------------------------------------------------- #

SMALL = DiagnosticLimits(
    snapshots=3,
    jobs=3,
    operations=3,
    webhooks=3,
    degradations=3,
    reasons_per_snapshot=2,
    errors=3,
    repositories=2,
    data_flow_repositories=1,
)


def _small_bundle(review_root: Path) -> dict[str, Any]:
    facts = local_diagnostic_facts(
        review_root=review_root,
        protocol=ProtocolFacts(server_reason="server_not_configured"),
        data_flow=DataFlowFacts(unavailable="not_a_repository"),
        limits=SMALL,
    )
    return _build(facts, limits=SMALL)


@pytest.mark.parametrize("extra", [0, 1], ids=["at_limit", "past_limit"])
def test_snapshots_degradations_and_repositories_are_bounded(tmp_path: Path, extra: int) -> None:
    review_root = tmp_path / "reviews"
    # Empty partitions rank after the partition holding the Reviews (durable updated_at, not file time).
    for index in range(SMALL.repositories - 1 + extra):
        _partition(review_root, f"repo-{index}-empty")
    db = _partition(review_root, "repo-a")
    for index in range(SMALL.snapshots + extra):
        _plant_review(db, f"rv-{index}", minutes=index, runs=0, syntheses=0, deliveries=0)
    sections = _small_bundle(review_root)["sections"]
    assert [row["review_id"] for row in sections["snapshots"]["records"]] == [
        f"rv-{index}" for index in range(SMALL.snapshots)
    ]
    assert sections["snapshots"]["truncated"] is bool(extra) and sections["snapshots"]["limit"] == SMALL.snapshots
    assert len(sections["degradations"]["records"]) == SMALL.degradations
    assert all(len(row["reasons"]) <= SMALL.reasons_per_snapshot for row in sections["degradations"]["records"])
    bundle = _small_bundle(review_root)
    assert bundle["scope"]["repositories_scanned"] == SMALL.repositories
    assert bundle["scope"]["repositories_total"] == SMALL.repositories + extra


@pytest.mark.parametrize("extra", [0, 1], ids=["at_limit", "past_limit"])
def test_degraded_snapshots_and_their_reasons_are_bounded(tmp_path: Path, extra: int) -> None:
    limits = DiagnosticLimits(snapshots=6, degradations=2, reasons_per_snapshot=2)
    review_root = tmp_path / "reviews"
    db = _partition(review_root, "repo-a")
    reasons = ["packet degraded", "diff truncated", "index stale"][: limits.reasons_per_snapshot + extra]
    for index in range(limits.degradations + extra):
        _plant_review(db, f"rv-d{index}", minutes=index, runs=0, syntheses=0, deliveries=0, degraded=reasons)
    for index in range(2):
        _plant_review(db, f"rv-ok{index}", minutes=10 + index, runs=0, syntheses=0, deliveries=0, degraded=())
    facts = local_diagnostic_facts(
        review_root=review_root,
        protocol=ProtocolFacts(server_reason="server_not_configured"),
        data_flow=DataFlowFacts(unavailable="not_a_repository"),
        limits=limits,
    )
    degradations = _build(facts, limits=limits)["sections"]["degradations"]
    assert [row["review_id"] for row in degradations["records"]] == ["rv-d0", "rv-d1"]
    assert degradations["truncated"] is bool(extra) and degradations["limit"] == limits.degradations
    for row in degradations["records"]:
        assert len(row["reasons"]) == limits.reasons_per_snapshot
        assert row["reasons_total"] == limits.reasons_per_snapshot + extra


@pytest.mark.parametrize("extra", [0, 1], ids=["at_limit", "past_limit"])
def test_jobs_operations_and_errors_are_bounded(tmp_path: Path, extra: int) -> None:
    review_root = tmp_path / "reviews"
    db = _partition(review_root, "repo-a")
    _plant_review(db, "rv-0", minutes=1, runs=SMALL.jobs + extra, syntheses=0, deliveries=SMALL.operations + extra)
    sections = _small_bundle(review_root)["sections"]
    for name, limit in (("intelligence_jobs", SMALL.jobs), ("provider_operations", SMALL.operations)):
        assert len(sections[name]["records"]) == limit, name
        assert sections[name]["truncated"] is bool(extra), name
    errors = sections["recent_errors"]
    assert len(errors["records"]) == SMALL.errors and errors["truncated"] is True


@pytest.mark.parametrize("extra", [0, 1], ids=["at_limit", "past_limit"])
def test_errors_are_bounded_at_and_past_the_limit(tmp_path: Path, extra: int) -> None:
    review_root = tmp_path / "reviews"
    db = _partition(review_root, "repo-a")
    _plant_review(db, "rv-0", minutes=1, runs=SMALL.errors + extra, syntheses=0, deliveries=0)
    errors = _small_bundle(review_root)["sections"]["recent_errors"]
    assert len(errors["records"]) == SMALL.errors and errors["truncated"] is bool(extra)


class _FakeReader:
    """An in-memory ReviewStoreReader with *count* of everything."""

    def __init__(self, count: int) -> None:
        self.count = count
        self.asked: list[tuple[str, int]] = []

    def recent_sessions(self, limit: int) -> list[SessionRow]:
        self.asked.append(("sessions", limit))
        return [SessionRow(f"rv-{i}", "open", f"sn-{i}", _at(i)) for i in range(min(self.count, limit))]

    def snapshot(self, session: SessionRow) -> SnapshotRecord:
        return SnapshotRecord(session.review_id, "repo-a", "open", session.current_revision_id)

    def intelligence_jobs(self, review_id: str, limit: int) -> list[IntelligenceJobRecord]:
        self.asked.append(("jobs", limit))
        return []

    def provider_operations(self, review_id: str, limit: int) -> list[ProviderOperationRecord]:
        self.asked.append(("operations", limit))
        return []

    def webhook_deliveries(self, limit: int) -> list[WebhookDeliveryRecord]:
        self.asked.append(("webhooks", limit))
        n = min(self.count, limit)
        return [WebhookDeliveryRecord(f"dl-{i}", "repo-a", "", "github", "pull_request", "failed") for i in range(n)]


@pytest.mark.parametrize("extra", [0, 1], ids=["at_limit", "past_limit"])
def test_readers_are_asked_for_at_most_limit_plus_one_and_webhooks_are_bounded(extra: int) -> None:
    reader = _FakeReader(SMALL.webhooks + extra)
    records = collect_review_records([("repo-a", reader)], limits=SMALL, webhooks=True)
    assert set(reader.asked) == {
        ("sessions", SMALL.snapshots + 1),
        ("jobs", SMALL.jobs + 1),
        ("operations", SMALL.operations + 1),
        ("webhooks", SMALL.webhooks + 1),
    }
    facts = DiagnosticFacts(
        surface="hosted",
        versions=VersionFacts(),
        protocol=ProtocolFacts(client_reason="not_applicable_hosted", server_identity={"name": "x"}),
        data_flow=DataFlowFacts(unavailable="no_repositories"),
        records=records,
        queue_jobs=RecordSource(
            tuple(QueueJobRecord(f"job-{i}", "repo-a", state="failed") for i in range(SMALL.jobs + extra))
        ),
    )
    sections = _build(facts, limits=SMALL)["sections"]
    for name, limit in (("webhook_deliveries", SMALL.webhooks), ("queue_jobs", SMALL.jobs)):
        assert len(sections[name]["records"]) == limit and sections[name]["truncated"] is bool(extra), name


# --------------------------------------------------------------------------- #
# the redaction pass
# --------------------------------------------------------------------------- #


def test_redact_text_keeps_only_lexicon_words_short_numbers_and_punctuation() -> None:
    assert redact_text("Provider request FAILED: timeout after 30 retries") == (
        "provider request failed: timeout after 30 retries"
    )
    assert redact_text(f"failed at /home/alice/src with {API_KEY} code 12345") == (
        f"failed at /{PLACEHOLDER} with {PLACEHOLDER} code {PLACEHOLDER}"
    )
    long = redact_text("error " * 400)
    assert len(long) == MAX_TEXT_CHARS and long.endswith(PLACEHOLDER)
    _assert_clean(redact_text(CONTENT + ERROR_TEXT))
    assert ERROR_MARK.lower() not in redact_text(ERROR_TEXT).lower()


def test_redact_text_keeps_no_non_ascii_text() -> None:
    out = redact_text(f"failed {NON_ASCII_SOURCE} {FULLWIDTH_KEY} timeout")
    assert out.isascii() and out.startswith("failed") and out.endswith("timeout")
    _assert_clean(out)


def test_redact_text_drops_a_run_its_input_cap_splits() -> None:
    # A long number or a mixed run cut at the cap must not pass as a short number or a lexicon word.
    assert redact_text(" " * (MAX_TEXT_INPUT_CHARS - 2) + "12345678") == ""
    assert redact_text(" " * (MAX_TEXT_INPUT_CHARS - 5) + "errorQ7xZ") == ""
    assert redact_text("failed " + " " * (MAX_TEXT_INPUT_CHARS - 9) + "12 345678") == "failed 12"


def test_included_error_text_is_scrubbed_and_bounded() -> None:
    out = scrub_included_text(ERROR_TEXT, environ={ENV_NAME: ENV_VALUE})
    assert ERROR_MARK in out and f"<env:{ENV_NAME}>" in out
    for marker in SECRET_MARKERS:
        assert marker.lower() not in out.lower()
    assert len(scrub_included_text("failed " * 1000, environ={})) <= 1000
    # A full-width key whose segments are too short for the run rule is caught once NFKC makes it ASCII.
    wide = _fullwidth(SETTING_KEY)
    out = scrub_included_text(f"failed {wide} now", environ={})
    assert not [piece for piece in _fragments(SETTING_KEY_BODY, _fullwidth(SETTING_KEY_BODY)) if piece in out], out


SPLIT_SECRET = "Zq9xWv3Lp0Rt5Yk8Mn2B"
#: (text planted, its sensitive parts, environment) per secret shape.
SPLIT_CASES: dict[str, tuple[str, tuple[str, ...], dict[str, str]]] = {
    "env_value": (SPLIT_SECRET, (SPLIT_SECRET,), {"MY_TOKEN": SPLIT_SECRET}),
    "env_value_with_separators": (ENV_VALUE, (ENV_VALUE,), {ENV_NAME: ENV_VALUE}),
    "api_key": (f"sk-proj-{SPLIT_SECRET}", (SPLIT_SECRET,), {}),
    "bearer": (f"Authorization: Bearer {SPLIT_SECRET}", (SPLIT_SECRET,), {}),
    "url_userinfo": (f"https://u:{URL_PASSWORD}@h.invalid/x?t={URL_QUERY}", (URL_PASSWORD, URL_QUERY), {}),
}


def _fragments(*parts: str) -> list[str]:
    """Every 4-character piece of each sensitive part."""

    return [part[index : index + 4] for part in parts for index in range(len(part) - 3)]


def test_included_error_text_keeps_no_part_of_a_secret_its_input_cut_splits() -> None:
    for shape in sorted(SPLIT_CASES):
        secret, parts, environ = SPLIT_CASES[shape]
        cap = 4 * MAX_INCLUDED_TEXT_CHARS
        for offset in range(cap - len(secret) - 2, cap + 2):
            out = scrub_included_text(" " * offset + secret + " tail", environ=environ)
            assert not [piece for piece in _fragments(*parts) if piece in out], (shape, offset - cap)
        probe = scrub_included_text(" " * 3990 + SPLIT_SECRET + " tail", environ={"MY_TOKEN": SPLIT_SECRET})
        assert probe == "<env:MY_TOKEN>"


def test_overlapping_environment_values_leave_no_fragment() -> None:
    first, second = "Kp3vLm8qRt", "8qRt5wXz2Nc"
    out = scrub_included_text("failed at Kp3vLm8qRt5wXz2Nc now", environ={"FIRST_TOKEN": first, "SECOND_TOKEN": second})
    assert not [piece for piece in _fragments(first, second) if piece in out], out
    assert out.startswith("failed at <env:") and out.endswith("now")
    # One value inside another: the outer one is replaced whole, nothing after the inner one returns.
    outer, inner = "Qz7Lm8qRt5Nc9Wv", "Lm8qRt5N"
    out = scrub_included_text(f"failed at {outer} now", environ={"OUTER_TOKEN": outer, "INNER_TOKEN": inner})
    assert out == "failed at <env:OUTER_TOKEN> now", out


SHORT_ENV_VALUE = "Kq7mVx2Lpz"


def test_a_full_width_spelling_of_an_environment_value_is_replaced() -> None:
    # The reported probe: NFKC ran after the environment pass, so the full-width form came out as the ASCII value.
    wide = _fullwidth(SHORT_ENV_VALUE)
    for planted, configured in ((wide, SHORT_ENV_VALUE), (SHORT_ENV_VALUE, wide), (wide, wide)):
        out = scrub_included_text(f"failed with {planted} now", environ={"APP_PW": configured})
        assert out == "failed with <env:APP_PW> now", (planted == wide, configured == wide)


#: (unit, raw length, normalized length): NFKC grows a ligature, shrinks a combining pair, keeps a space.
#: The spaced pair shrinks the window below the cut while its output collapses, so the tail is still shown.
NORMALIZATION_UNITS = {
    "ligature": ("\ufb01", 1, 2),
    "combining": ("e\u0301", 2, 1),
    "spaced-combining": ("e\u0301" + " " * 10, 12, 11),
    "space": (" ", 1, 1),
}


def test_normalization_keeps_the_straddle_guarantee() -> None:
    for unit_name in sorted(NORMALIZATION_UNITS):
        unit, raw_width, normalized_width = NORMALIZATION_UNITS[unit_name]
        cap = 4 * MAX_INCLUDED_TEXT_CHARS
        read = cap + len(SHORT_ENV_VALUE) + 1
        counts = {
            *range(cap // normalized_width - 12, cap // normalized_width + 3),
            *range(read // raw_width - 12, read // raw_width + 3),
        }
        wide = _fullwidth(SHORT_ENV_VALUE)
        for count in sorted(counts):
            for gap in range(1, raw_width + 1):
                for planted in (SHORT_ENV_VALUE, wide):
                    text = unit * count + " " * gap + planted + " tail"
                    out = scrub_included_text(text, environ={"APP_PW": SHORT_ENV_VALUE})
                    assert not [piece for piece in _fragments(SHORT_ENV_VALUE, wide) if piece in out], (count, gap)
                    assert len(out) <= MAX_INCLUDED_TEXT_CHARS


def test_normalization_reads_a_bounded_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    real = unicodedata.normalize
    seen: list[int] = []

    def normalize(form: Any, text: str) -> str:
        seen.append(len(text))
        return real(form, text)

    monkeypatch.setattr(unicodedata, "normalize", normalize)
    out = scrub_included_text("\ufb01" * 1_000_000, environ={"APP_PW": SHORT_ENV_VALUE})
    assert len(out) <= MAX_INCLUDED_TEXT_CHARS
    assert seen and max(seen) <= 4 * MAX_INCLUDED_TEXT_CHARS + len(SHORT_ENV_VALUE) + 1


def test_fingerprints_are_keyed_per_bundle(tmp_path: Path) -> None:
    facts = _builder_facts(tmp_path)
    first, second = (
        build_diagnostic_bundle(facts, allowed_includes=LOCAL_INCLUDES, requested_by="local") for _ in range(2)
    )
    ours = [row["fingerprint"] for row in first["sections"]["recent_errors"]["records"]]
    theirs = [row["fingerprint"] for row in second["sections"]["recent_errors"]["records"]]
    # The same message: one fingerprint within a bundle, a different one in the next bundle.
    assert len(ours) >= 2 and len(set(ours)) == 1 and len(set(theirs)) == 1
    assert set(ours) != set(theirs)
    assert [row["message"] for row in first["sections"]["recent_errors"]["records"]] == [
        row["message"] for row in second["sections"]["recent_errors"]["records"]
    ]


def test_ids_and_enums_shaped_like_credentials_are_withheld() -> None:
    facts = DiagnosticFacts(
        surface="local",
        versions=VersionFacts(components=(("lemoncrow", "1.2.3"),)),
        protocol=ProtocolFacts(server_reason="server_not_configured"),
        data_flow=DataFlowFacts(unavailable="not_a_repository"),
        records=collect_review_records([], webhooks=False, unavailable="no_review_store"),
        queue_jobs=RecordSource(
            (
                QueueJobRecord(API_KEY.lower(), "repo-a", capability=API_KEY.lower(), state="queued"),
                QueueJobRecord("job-1", f"repo {COMMENT}", capability="a" * 49, state="q7vxk2mn9plr4tzw8ybc"),
            )
        ),
    )
    text = _dump(_build(facts))
    _assert_clean(text)
    records = json.loads(text)["sections"]["queue_jobs"]["records"]
    by_id = {row["job_id"]: row for row in records}
    assert by_id[WITHHELD]["capability"] == "other" and by_id[WITHHELD]["state"] == "queued"
    assert by_id["job-1"]["repo_id"] == WITHHELD and by_id["job-1"]["capability"] == "other"
    assert by_id["job-1"]["state"] == "other"
