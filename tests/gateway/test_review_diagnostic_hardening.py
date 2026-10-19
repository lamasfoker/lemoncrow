"""RC10: diagnostic bundle policy hardening (shared builder and the local surface).

Adversarial redaction (a secret split across lines or fields, overlapping,
full-width or lookalike, equal to or inside a model id or endpoint name,
embedded in a URL), repository selection and ordering, and the sections that
report worker protocol, execution locus, gates and the not-yet-reported job
lifecycle. Every secret here is a synthetic string generated for the test.
"""

from __future__ import annotations

import json
import os
import sqlite3
import unicodedata
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli import cli
from lemoncrow.pro.capabilities.review import diagnostic_local
from lemoncrow.pro.capabilities.review.data_flow import DataFlowProjection, FlowOrigin, FlowRow
from lemoncrow.pro.capabilities.review.diagnostic_bundle import (
    DEFAULT_LIMITS,
    LOCAL_INCLUDES,
    MAX_INCLUDED_TEXT_CHARS,
    WITHHELD,
    DataFlowFacts,
    DiagnosticFacts,
    DiagnosticLimits,
    IntelligenceJobRecord,
    ProtocolFacts,
    RecordSource,
    ReviewRecords,
    VersionFacts,
    build_diagnostic_bundle,
    scrub_included_text,
)
from lemoncrow.pro.capabilities.review.diagnostic_local import local_diagnostic_facts
from lemoncrow.pro.capabilities.review.diagnostic_secrets import SecretGuard, match_key

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
SALT = b"rc10-test-salt"


def _fullwidth(text: str) -> str:
    return "".join(chr(ord(char) + 0xFEE0) for char in text)


def _fragments(*parts: str, width: int = 4) -> list[str]:
    return [part[index : index + width] for part in parts for index in range(len(part) - width + 1)]


# --------------------------------------------------------------------------- #
# the matcher
# --------------------------------------------------------------------------- #

SECRET = "Quartz-Heron-47-Basalt"


def test_match_key_folds_width_case_whitespace_zero_width_and_lookalikes() -> None:
    plain = match_key(SECRET)[0]
    assert plain == "quartz-heron-47-basalt"
    for variant in (
        _fullwidth(SECRET),
        SECRET.upper(),
        "Quartz-Heron-\n   47-Basalt",
        "Quartz\u200b-Heron-47\u00ad-Basalt",
        # Cyrillic a, e, o and p inside the secret.
        SECRET.replace("a", "\u0430").replace("e", "\u0435").replace("o", "\u043e"),
        # Greek omicron and alpha.
        SECRET.replace("o", "\u03bf").replace("a", "\u03b1"),
    ):
        assert match_key(variant)[0] == plain, variant


def test_match_key_origins_map_a_key_slice_back_to_the_source_span() -> None:
    text = "ab \u200b cd  ef"
    key, origins = match_key(text)
    assert key == "abcdef"
    assert text[origins[2] : origins[3] + 1] == "cd"
    assert origins == sorted(origins)


def test_the_guard_collects_secret_named_variables_and_url_credentials_only() -> None:
    guard = SecretGuard.from_environ(
        {
            "LAB_API_KEY": "orchid-gateway",
            "PLAIN_MODEL": "claude-opus-plain",
            "UPSTREAM": "https://dave:pw-Zq8x@host.invalid/v1?token=tok-Lm5nQ",
            "SHORT_TOKEN": "abc",
        },
        extra=("operator-secret-9",),
    )
    assert guard.overlaps("orchid-gateway")
    assert guard.overlaps("pw-Zq8x") and guard.overlaps("tok-Lm5nQ") and guard.overlaps("operator-secret-9")
    assert not guard.overlaps("claude-opus-plain"), "a non-secret setting is not a secret"
    assert not guard.overlaps("abc"), "a value under the floor is not tracked"


def test_overlap_is_equal_contains_and_contained_but_not_for_tiny_names() -> None:
    guard = SecretGuard.from_environ({"LAB_API_KEY": "orchid-gateway-77"})
    assert guard.overlaps("orchid-gateway-77")
    assert guard.overlaps("orchid-gateway")  # a substring of the secret
    assert guard.overlaps("my-orchid-gateway-77-model")  # contains the secret
    assert guard.overlaps(_fullwidth("Orchid-Gateway-77"))
    assert not guard.overlaps("ai"), "a two-letter name is vocabulary"
    assert not guard.overlaps("gpt-4o-mini")


# --------------------------------------------------------------------------- #
# included error text: split, overlapping, lookalike
# --------------------------------------------------------------------------- #

VALUE = "plum-violet-Q9-heron-K2-amber"


@pytest.mark.parametrize(
    "shape",
    ["newline", "indent", "zero_width", "lookalike", "fullwidth", "upper", "carriage", "tab_runs"],
)
def test_error_text_removes_a_configured_secret_however_it_is_split_or_disguised(shape: str) -> None:
    variants = {
        "newline": VALUE[:10] + "\n" + VALUE[10:],
        "indent": VALUE[:10] + "\n      " + VALUE[10:],
        "zero_width": VALUE[:10] + "\u200b\u2060" + VALUE[10:],
        "lookalike": VALUE.replace("a", "\u0430").replace("e", "\u0435"),
        "fullwidth": _fullwidth(VALUE),
        "upper": VALUE.upper(),
        "carriage": VALUE[:5] + "\r\n" + VALUE[5:20] + "\r\n" + VALUE[20:],
        "tab_runs": VALUE.replace("-", "\t-\t"),
    }
    out = scrub_included_text(f"request failed: {variants[shape]} (retrying)", environ={"APP_TOKEN": VALUE})
    assert out == "request failed: <env:APP_TOKEN> (retrying)", (shape, out)


def test_error_text_removes_a_secret_split_across_two_fields_joined_by_the_caller() -> None:
    first, second = VALUE[:11], VALUE[11:]
    out = scrub_included_text(f"left={first}\nright={second}", environ={"APP_TOKEN": VALUE})
    assert not [piece for piece in _fragments(first, second) if piece in out], out


def test_error_text_removes_overlapping_secrets_even_when_split() -> None:
    first, second = "Kp3vLm8qRt", "8qRt5wXz2Nc"
    out = scrub_included_text(
        "failed at Kp3vLm\n8qRt5wXz2Nc now", environ={"FIRST_TOKEN": first, "SECOND_TOKEN": second}
    )
    assert not [piece for piece in _fragments(first, second) if piece in out], out


def test_error_text_removes_url_embedded_credentials_and_the_guards_extras() -> None:
    url = "https://dave:pw-Zq8x@host.invalid/v1?token=tok-Lm5nQ"
    environ = {"UPSTREAM": url}
    text = "failed dave pw-Zq8x then tok-Lm5nQ and operator-secret-9 end"
    guard = SecretGuard.from_environ(environ, extra=("operator-secret-9",))
    out = scrub_included_text(text, environ=environ, guard=guard)
    for leaked in ("pw-Zq8x", "tok-Lm5nQ", "operator-secret-9"):
        assert leaked not in out, out


def test_a_secret_a_long_gap_splits_across_the_cut_leaves_no_prefix() -> None:
    cap = 4 * MAX_INCLUDED_TEXT_CHARS
    for lead in range(cap - len(VALUE) - 8, cap + 2, 3):
        text = " " * lead + VALUE[:9] + " " * 600 + VALUE[9:] + " tail"
        out = scrub_included_text(text, environ={"APP_TOKEN": VALUE})
        assert not [piece for piece in _fragments(VALUE) if piece in out], (lead - cap, out)


def test_a_short_secret_split_across_the_cut_leaves_no_prefix_without_the_fragment_defense() -> None:
    # Under 12 characters the six-character fragment defense does not apply: only the
    # dangling-prefix rule removes what precedes a cut that a secret continues past.
    short = "zq-ntr-481"
    cap = 4 * MAX_INCLUDED_TEXT_CHARS
    for lead in range(cap - len(short) - 8, cap + 2, 2):
        text = " " * lead + short[:7] + " " * 600 + short[7:] + " tail"
        out = scrub_included_text(text, environ={"APP_TOKEN": short})
        assert not [piece for piece in _fragments(short[:7]) if piece in out], (lead - cap, out)


# --------------------------------------------------------------------------- #
# model names and endpoint names
# --------------------------------------------------------------------------- #


def _facts(*, model: str = "", flow: DataFlowFacts | None = None) -> DiagnosticFacts:
    job = IntelligenceJobRecord(
        job_id="job-1",
        kind="intelligence",
        review_id="rv-1",
        repo_id="repo-a",
        state="failed",
        created_at=NOW.isoformat(),
        model=model,
        error="failed",
    )
    return DiagnosticFacts(
        surface="local",
        versions=VersionFacts(),
        protocol=ProtocolFacts(server_reason="server_not_configured"),
        data_flow=flow or DataFlowFacts(unavailable="not_a_repository"),
        records=ReviewRecords(
            snapshots=RecordSource(()),
            intelligence_jobs=RecordSource((job,)),
            provider_operations=RecordSource(()),
            webhook_deliveries=RecordSource(unavailable="no_provider_webhooks"),
            repositories_scanned=1,
        ),
        queue_jobs=RecordSource(unavailable="no_durable_queue"),
    )


def _model(model: str, environ: Mapping[str, str], include: tuple[str, ...] = ("model-names",)) -> str:
    bundle = build_diagnostic_bundle(
        _facts(model=model),
        include=include,
        allowed_includes=LOCAL_INCLUDES,
        requested_by="local",
        now=NOW,
        salt=SALT,
        environ=environ,
    )
    return str(bundle["sections"]["intelligence_jobs"]["records"][0].get("model", "<absent>"))


ENV = {"LAB_API_KEY": "orchid-gateway-77", "PLAIN": "gpt-4o-mini"}


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gpt-4o-mini", "gpt-4o-mini"),
        ("openai/gpt-4o-mini", "openai/gpt-4o-mini"),
        ("ollama/qwen2.5:1.5b", "ollama/qwen2.5:1.5b"),
        # equal to, a substring of, and containing a configured secret
        ("orchid-gateway-77", WITHHELD),
        ("orchid-gateway", WITHHELD),
        ("llama-orchid-gateway-77", WITHHELD),
        ("ORCHID-GATEWAY-77", WITHHELD),
        # a non-ASCII id is not a model identifier at all: it never shows (the shared model-id cleaner drops it)
        (_fullwidth("orchid-gateway-77"), ""),
        ("orch" + chr(0x0456) + "d-gateway-77", ""),
        # not a model identifier the data-flow allowlist shows
        ("internal-gw.corp.example", WITHHELD),
        ("unknownvendor/model-x", WITHHELD),
        ("sk-proj-Hq3Vt8Ln2Rx7Kw4Mz9Pd5Gs1", WITHHELD),
        ("", ""),
    ],
)
def test_model_names_follow_the_allowlist_and_withhold_secret_overlap(model: str, expected: str) -> None:
    assert _model(model, ENV) == expected


def test_model_names_are_absent_without_the_include_even_when_clean() -> None:
    assert _model("gpt-4o-mini", ENV, include=()) == "<absent>"


def test_a_model_equal_to_a_non_secret_setting_is_shown() -> None:
    # PLAIN is not secret-named, so a configured model id that equals it is not a leak.
    assert _model("gpt-4o-mini", {"PLAIN": "gpt-4o-mini"}) == "gpt-4o-mini"


def test_a_model_equal_to_a_url_embedded_credential_is_withheld() -> None:
    environ = {"UPSTREAM": "https://dave:pw-Zq8x-gw@host.invalid/v1"}
    assert _model("pw-Zq8x-gw", environ) == WITHHELD
    assert _model("pw-Zq8x", environ) == WITHHELD


def _projection(*rows: FlowRow) -> DataFlowFacts:
    return DataFlowFacts(projections=(("repo-a", DataFlowProjection("local", "repo-a", rows)),))


def _row(row_id: str, setting: str) -> FlowRow:
    return FlowRow(
        row_id, row_id.split(".", 1)[0], "subject", "fact", "detail", (FlowOrigin(setting, "built_in_default"),)
    )


@pytest.mark.parametrize(
    "name",
    [
        "orchid-gateway-77",
        "orchid-gateway",
        "x-orchid-gateway-77-y",
        _fullwidth("orchid-gateway-77"),
        "ORCHID-GATEWAY-77",
    ],
)
def test_endpoint_names_that_overlap_a_secret_are_withheld_in_ids_and_origins(name: str) -> None:
    facts = _facts(
        flow=_projection(
            _row(f"models.endpoint.{name}", f"providers.json endpoints.{name}"),
            _row("models.endpoint.lab", "providers.json endpoints.lab"),
        )
    )
    bundle = build_diagnostic_bundle(
        facts, allowed_includes=LOCAL_INCLUDES, requested_by="local", now=NOW, salt=SALT, environ=ENV
    )
    text = json.dumps(bundle, ensure_ascii=False).lower()
    assert "orchid" not in text and "gateway" not in text and match_key(name)[0] not in match_key(text)[0]
    rows = bundle["sections"]["data_flow"]["repositories"][0]["rows"]
    assert rows[0]["id"] == WITHHELD and rows[0]["origins"][0]["setting"] == WITHHELD
    assert rows[1]["id"] == "models.endpoint.lab", "an unrelated endpoint keeps its name"


def test_vocabulary_ids_are_not_withheld_by_a_short_overlap() -> None:
    # "ai" sits inside the secret "main-secret"; a two-letter segment is vocabulary, not a leak.
    facts = _facts(flow=_projection(_row("execution.locus.ai", "LEMONCROW_AI")))
    bundle = build_diagnostic_bundle(
        facts,
        allowed_includes=LOCAL_INCLUDES,
        requested_by="local",
        now=NOW,
        salt=SALT,
        environ={"MAIN_SECRET": "main-secret-9"},
    )
    assert bundle["sections"]["data_flow"]["repositories"][0]["rows"][0]["id"] == "execution.locus.ai"


# --------------------------------------------------------------------------- #
# the whole local bundle, end to end
# --------------------------------------------------------------------------- #


def test_the_local_bundle_never_carries_a_secret_named_endpoint_or_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.gateway.test_review_diagnostic_bundle import _git_repo, _partition, _plant_review

    root = tmp_path / "root"
    root.mkdir()
    secret = "orchid-gateway-77"
    (root / "providers.json").write_text(
        json.dumps(
            {
                "custom": {
                    "endpoints": {
                        secret: {
                            "base_url": "https://models.example.invalid/v1",
                            "models": {secret: {}, "gpt-4o-mini": {}},
                        },
                        "lab": {"base_url": "https://models.example.invalid/v1", "models": {"qwen-coder": {}}},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    review_root = root / "server" / "data" / "reviews"
    _plant_review(_partition(review_root, "repo-alpha"), "rv-alpha-1", minutes=10)
    monkeypatch.delenv("LEMONCROW_SERVER_REVIEW_ROOT", raising=False)
    monkeypatch.setenv("LAB_API_KEY", secret)
    from lemoncrow.pro.capabilities.review import diagnostic_local

    monkeypatch.setattr(diagnostic_local, "discover_server", lambda _config: (_ for _ in ()).throw(OSError("down")))
    repo = _git_repo(tmp_path / "repo")
    for include in ((), ("--include", "model-names"), ("--include", "error-text", "--include", "model-names")):
        result = CliRunner().invoke(
            cli,
            ["--root", str(root), "review", "diagnostics", "--repo", str(repo), *include],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output[:300]
        assert match_key(secret)[0] not in match_key(result.output)[0], include
        assert "orchid" not in result.output.lower()
        rows = json.loads(result.output)["sections"]["data_flow"]["repositories"][0]["rows"]
        assert WITHHELD in {row["id"] for row in rows} or all(secret not in row["id"] for row in rows)


def test_include_state_is_recorded_for_the_audit_trail() -> None:
    bundle = build_diagnostic_bundle(
        _facts(model="gpt-4o-mini"),
        include=("error-text",),
        allowed_includes=LOCAL_INCLUDES,
        requested_by="local",
        now=NOW,
        salt=SALT,
        environ={},
    )
    assert bundle["include"]["requested"] == ["error-text"]
    assert os.environ is not None and sqlite3.sqlite_version
    assert timedelta(0) == timedelta(0)


# --------------------------------------------------------------------------- #
# repository selection: explicit id, else durable updated_at; never id or file order
# --------------------------------------------------------------------------- #

ONE = DiagnosticLimits(repositories=1)


def _local(review_root: Path, **kwargs: Any) -> dict[str, Any]:
    facts = local_diagnostic_facts(
        review_root=review_root,
        protocol=ProtocolFacts(server_reason="server_not_configured"),
        data_flow=DataFlowFacts(unavailable="not_a_repository"),
        **kwargs,
    )
    return build_diagnostic_bundle(
        facts,
        allowed_includes=LOCAL_INCLUDES,
        requested_by="local",
        now=NOW,
        salt=SALT,
        environ={},
        limits=kwargs.get("limits", DEFAULT_LIMITS),
    )


def _repos(bundle: Mapping[str, Any]) -> list[str]:
    return sorted({row["repo_id"] for row in bundle["sections"]["snapshots"]["records"]})


def _plant(review_root: Path, repo_id: str, review_id: str, *, minutes: int) -> Path:
    from tests.gateway.test_review_diagnostic_bundle import _partition, _plant_review

    db = _partition(review_root, repo_id)
    _plant_review(db, review_id, minutes=minutes, runs=0, syntheses=0, deliveries=0)
    return db


def test_the_local_recency_is_the_durable_updated_at_not_the_file_mtime(tmp_path: Path) -> None:
    review_root = tmp_path / "reviews"
    new_db = _plant(review_root, "repo-new", "rv-new", minutes=1)
    old_db = _plant(review_root, "repo-old", "rv-old", minutes=500)
    # Maintenance touched the stale store last: by file time it is the newest.
    os.utime(new_db, (1_000_000, 1_000_000))
    os.utime(old_db, (2_000_000_000, 2_000_000_000))
    assert old_db.stat().st_mtime > new_db.stat().st_mtime
    bundle = _local(review_root, limits=ONE)
    assert _repos(bundle) == ["repo-new"]
    assert bundle["scope"]["repositories_scanned"] == 1 and bundle["scope"]["repositories_total"] == 2


def test_the_local_selection_is_not_id_order_in_either_direction(tmp_path: Path) -> None:
    for newest, oldest in (("repo-a", "repo-z"), ("repo-z", "repo-a")):
        review_root = tmp_path / newest / "reviews"
        _plant(review_root, newest, "rv-1", minutes=1)
        _plant(review_root, oldest, "rv-2", minutes=900)
        assert _repos(_local(review_root, limits=ONE)) == [newest]


def test_the_local_selection_takes_the_n_most_recent_of_many(tmp_path: Path) -> None:
    review_root = tmp_path / "reviews"
    many, kept = 9, 6
    # Ids and creation order are shuffled against recency.
    names = [f"repo-{(index * 4) % many:02d}" for index in range(many)]
    for index, name in enumerate(names):
        _plant(review_root, name, f"rv-{index:02d}", minutes=1 + index)
    bundle = _local(review_root, limits=DiagnosticLimits(repositories=kept, snapshots=4))
    assert bundle["scope"]["repositories_scanned"] == kept and bundle["scope"]["repositories_total"] == many
    seen = {row["repo_id"] for row in bundle["sections"]["snapshots"]["records"]}
    assert seen <= set(names[:kept]) and not seen & set(names[kept:])
    # The snapshots shown are the newest across every scanned repository.
    assert [row["review_id"] for row in bundle["sections"]["snapshots"]["records"]] == [
        f"rv-{index:02d}" for index in range(4)
    ]


def test_a_store_with_no_review_ranks_after_every_store_with_one(tmp_path: Path) -> None:
    from tests.gateway.test_review_diagnostic_bundle import _partition

    review_root = tmp_path / "reviews"
    _partition(review_root, "repo-0-empty")
    _plant(review_root, "repo-z", "rv-1", minutes=4000)
    assert _repos(_local(review_root, limits=ONE)) == ["repo-z"]


def test_an_explicit_repository_id_reads_that_store_alone_even_when_it_is_the_oldest(tmp_path: Path) -> None:
    review_root = tmp_path / "reviews"
    _plant(review_root, "repo-new", "rv-new", minutes=1)
    _plant(review_root, "repo-old", "rv-old", minutes=900)
    bundle = _local(review_root, repository_id="repo-old")
    assert _repos(bundle) == ["repo-old"]
    assert bundle["scope"]["repositories_scanned"] == 1 and bundle["scope"]["repositories_total"] == 2


@pytest.mark.parametrize("repository_id", ["repo-missing", "..", "../reviews", "repo-new/../repo-old", "/etc"])
def test_an_unknown_or_path_shaped_repository_id_reads_nothing(tmp_path: Path, repository_id: str) -> None:
    review_root = tmp_path / "reviews"
    _plant(review_root, "repo-new", "rv-new", minutes=1)
    bundle = _local(review_root, repository_id=repository_id)
    assert bundle["sections"]["snapshots"]["status"] == "unavailable"
    assert bundle["scope"]["repositories_scanned"] == 0


def test_the_cli_selects_by_repository_id_and_refuses_an_unknown_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    review_root = root / "server" / "data" / "reviews"
    _plant(review_root, "repo-new", "rv-new", minutes=1)
    _plant(review_root, "repo-old", "rv-old", minutes=900)
    monkeypatch.delenv("LEMONCROW_SERVER_REVIEW_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    ok = CliRunner().invoke(cli, ["--root", str(root), "review", "diagnostics", "--repository-id", "repo-old"])
    assert ok.exit_code == 0, ok.output[:300]
    assert _repos(json.loads(ok.output)) == ["repo-old"]
    bad = CliRunner().invoke(cli, ["--root", str(root), "review", "diagnostics", "--repository-id", "nope"])
    assert bad.exit_code != 0 and "--repository-id" in bad.output


def test_snapshots_are_ordered_by_instant_not_by_timestamp_spelling(tmp_path: Path) -> None:
    """``Z`` and ``+00:00`` spellings of different instants sort by instant, not by text."""

    from lemoncrow.pro.capabilities.review.diagnostic_bundle import SessionRow, SnapshotRecord, collect_review_records

    class Reader:
        def __init__(self, repo: str, stamp: str) -> None:
            self.repo, self.stamp = repo, stamp

        def recent_sessions(self, limit: int) -> list[SessionRow]:
            return [SessionRow(f"rv-{self.repo}", "open", "", self.stamp)]

        def snapshot(self, session: SessionRow) -> SnapshotRecord:
            return SnapshotRecord(session.review_id, self.repo, "open", review_updated_at=session.updated_at)

        def intelligence_jobs(self, review_id: str, limit: int) -> list[Any]:
            return []

        def provider_operations(self, review_id: str, limit: int) -> list[Any]:
            return []

        def webhook_deliveries(self, limit: int) -> list[Any]:
            return []

    # As text "…T10:00:00+00:00" < "…T10:00:00Z" < "…T09:30:00-05:00" is wrong: the last is 14:30Z, the newest.
    readers = [
        ("a", Reader("a", "2026-10-01T10:00:00+00:00")),
        ("b", Reader("b", "2026-10-01T09:30:00-05:00")),
        ("c", Reader("c", "2026-10-01T10:00:00Z")),
    ]
    records = collect_review_records(readers, limits=DiagnosticLimits(snapshots=2), webhooks=False)
    assert [row.review_id for row in records.snapshots.records] == ["rv-b", "rv-c"]


# --------------------------------------------------------------------------- #
# execution locus, gates, release, job lifecycle and budget
# --------------------------------------------------------------------------- #


def _bundle(facts: DiagnosticFacts, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    return build_diagnostic_bundle(
        facts,
        include=(),
        allowed_includes=LOCAL_INCLUDES,
        requested_by="tester",
        now=NOW,
        salt=SALT,
        environ=environ if environ is not None else {},
    )


def _with(facts: DiagnosticFacts, **changes: Any) -> DiagnosticFacts:
    from dataclasses import replace

    return replace(facts, **changes)


def _locus_row(row_id: str, locus: tuple[str, ...], status: str = "fact") -> FlowRow:
    return FlowRow(
        row_id,
        row_id.split(".", 1)[0],
        "subject",
        status,  # type: ignore[arg-type]
        "detail with /private/path and https://host.example/x",
        (FlowOrigin("review policy execution.review", "organization"),),
        "free-text reason that must not travel",
        locus,
    )


def test_the_bundle_schema_version_and_section_order_cover_the_new_sections() -> None:
    from lemoncrow.pro.capabilities.review.diagnostic_bundle import BUNDLE_SCHEMA_VERSION, SECTIONS

    bundle = _bundle(_facts())
    assert BUNDLE_SCHEMA_VERSION == 2 and bundle["schema_version"] == 2
    assert list(bundle["sections"]) == list(SECTIONS)
    for name in ("execution_locus", "gates", "job_lifecycle", "background_budget"):
        assert name in bundle["sections"]


def test_execution_locus_reuses_the_data_flow_projection_and_carries_closed_words_only() -> None:
    flow = _projection(
        _locus_row("execution.locus.review", ("managed", "customer_worker")),
        _locus_row("models.locus.default", ("customer_local",)),
        _locus_row("models.locus.other", ("not-a-word", "external", "https://host.example"), "unknown"),
        _row("models.endpoint.main", "providers.json"),
    )
    section = _bundle(_facts(flow=flow))["sections"]["execution_locus"]
    assert section["status"] == "populated"
    rows = {row["id"]: row for row in section["repositories"][0]["rows"]}
    assert set(rows) == {"execution.locus.review", "models.locus.default", "models.locus.other"}
    assert rows["execution.locus.review"] == {
        "id": "execution.locus.review",
        "kind": "execution",
        "status": "fact",
        "locus": ["customer_worker", "managed"],
    }
    assert rows["models.locus.default"]["kind"] == "model" and rows["models.locus.default"]["locus"] == [
        "customer_local"
    ]
    # an unlisted word and a URL are dropped, never echoed
    assert rows["models.locus.other"]["locus"] == ["external"]
    dumped = json.dumps(section)
    for leak in ("/private/path", "host.example", "free-text", "not-a-word"):
        assert leak not in dumped


def test_execution_locus_withholds_a_route_name_that_overlaps_a_secret() -> None:
    flow = _projection(_locus_row("models.locus.orchid-gateway-77", ("external",)))
    bundle = _bundle(_facts(flow=flow), {"LAB_API_KEY": "orchid-gateway-77"})
    assert "orchid-gateway" not in json.dumps(bundle)
    assert bundle["sections"]["execution_locus"]["repositories"][0]["rows"][0]["id"] == WITHHELD


def test_execution_locus_is_unavailable_with_the_data_flows_reason() -> None:
    section = _bundle(_facts())["sections"]["execution_locus"]
    assert section["status"] == "unavailable" and section["reason"] == "not_a_repository"


def test_gates_report_each_gate_and_release_is_explicitly_unknown_with_a_reason() -> None:
    from lemoncrow.pro.capabilities.review.diagnostic_bundle import OperationsFacts
    from lemoncrow.pro.capabilities.review.diagnostic_state import GateState

    operations = OperationsFacts(
        gates=(
            GateState(
                "gate_i",
                mode="report",
                approved=False,
                baseline_decision="accept",
                baseline_recorded_at="2026-09-30T10:00:00Z",
            ),
            GateState("gate_h", mode="enforce", approved=True),
            GateState("gate_x", unreadable=True),
        )
    )
    section = _bundle(_with(_facts(), operations=operations))["sections"]["gates"]
    gates = {row["gate"]: row for row in section["gates"]}
    assert gates["gate_i"] == {
        "gate": "gate_i",
        "status": "populated",
        "mode": "report",
        "thresholds_approved": False,
        "baseline": {"decision": "accept", "recorded_at": "2026-09-30T10:00:00.000000Z"},
    }
    assert gates["gate_h"]["mode"] == "enforce" and gates["gate_h"]["thresholds_approved"] is True
    assert gates["gate_x"]["status"] == "unknown" and gates["gate_x"]["reason"] == "gate_unreadable"
    assert section["release"] == {
        "state": "unknown",
        "reason": "release_not_reported",
        "detail": "release qualification state not yet reported",
    }


def test_gate_fields_are_sanitized_not_trusted() -> None:
    from lemoncrow.pro.capabilities.review.diagnostic_bundle import OperationsFacts
    from lemoncrow.pro.capabilities.review.diagnostic_state import GateState

    hostile = GateState(
        "gate_i",
        mode="https://evil.example/?k=AKIA" + "ABCDEFGHIJKLMNOP",
        approved=True,
        baseline_decision="a b c",
        baseline_recorded_at="not a time",
    )
    section = _bundle(_with(_facts(), operations=OperationsFacts(gates=(hostile,))))["sections"]["gates"]
    dumped = json.dumps(section)
    assert "evil.example" not in dumped and "AKIA" not in dumped
    assert section["gates"][0]["baseline"]["recorded_at"] == ""


def test_with_no_gate_state_collected_the_section_says_unknown_not_fine() -> None:
    section = _bundle(_facts())["sections"]["gates"]
    assert section["status"] == "unknown" and section["reason"] == "gates_not_reported"


def test_the_local_collector_reads_the_checkouts_gates(tmp_path: Path) -> None:
    facts = local_diagnostic_facts(
        review_root=tmp_path / "none",
        protocol=ProtocolFacts(server_reason="server_not_configured"),
        data_flow=DataFlowFacts(unavailable="not_a_repository"),
    )
    section = _bundle(facts)["sections"]["gates"]
    assert section["status"] == "populated"
    gates = {row["gate"]: row for row in section["gates"]}
    assert set(gates) == {"gate_i", "gate_h"}
    assert gates["gate_i"]["status"] == "populated" and gates["gate_i"]["mode"] in {"report", "enforce"}
    assert gates["gate_h"]["mode"] in {"report", "enforce"}
    assert section["release"]["state"] == "unknown"


def test_gate_states_degrade_to_unreadable_not_to_an_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review import diagnostic_state, evidence_qualification

    def boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("secret detail /etc/passwd")

    monkeypatch.setattr(evidence_qualification, "source_checkout_gate_dir", boom)
    monkeypatch.setattr(evidence_qualification, "packaged_contract_path", boom)
    states = diagnostic_state.gate_states()
    assert [(state.gate, state.unreadable) for state in states] == [("gate_i", True), ("gate_h", True)]


def test_job_lifecycle_and_background_budget_are_unknown_with_the_registered_reason() -> None:
    sections = _bundle(_facts())["sections"]
    assert sections["job_lifecycle"] == {
        "status": "unknown",
        "reason": "job_lifecycle_not_reported",
        "detail": "job lifecycle state not yet reported",
    }
    assert sections["background_budget"]["status"] == "unknown"
    assert sections["background_budget"]["reason"] == "background_budget_not_reported"


def test_a_provider_that_reports_lifecycle_state_is_sanitized_into_the_section() -> None:
    from lemoncrow.pro.capabilities.review.diagnostic_bundle import OperationsFacts, ReportedState

    operations = OperationsFacts(
        job_lifecycle=ReportedState(
            counts=(("waiting", 3), ("bad", -1), ("big", True)),  # type: ignore[arg-type]
            words=(("fairness", "deferred"), ("retention", "https://leak.example/x")),
        ),
        background_budget=ReportedState(words=(("state", "exhausted"),)),
    )
    sections = _bundle(_with(_facts(), operations=operations))["sections"]
    assert sections["job_lifecycle"] == {
        "status": "populated",
        "counts": {"waiting": 3},
        "states": {"fairness": "deferred", "retention": "other"},
    }
    assert sections["background_budget"]["states"] == {"state": "exhausted"}
    assert "leak.example" not in json.dumps(sections)


def test_the_builder_refuses_an_unregistered_release_reason() -> None:
    from lemoncrow.pro.capabilities.review.diagnostic_bundle import OperationsFacts
    from lemoncrow.pro.capabilities.review.diagnostic_state import GateState, ReleaseState

    operations = OperationsFacts(
        gates=(GateState("gate_h", mode="report", approved=False),),
        release=ReleaseState(unknown="free text with /path"),
    )
    with pytest.raises(ValueError, match="unregistered"):
        _bundle(_with(_facts(), operations=operations))


def test_a_bundle_with_a_known_release_state_reports_the_word() -> None:
    from lemoncrow.pro.capabilities.review.diagnostic_bundle import OperationsFacts
    from lemoncrow.pro.capabilities.review.diagnostic_state import GateState, ReleaseState

    operations = OperationsFacts(
        gates=(GateState("gate_h", mode="report", approved=False),), release=ReleaseState(unknown="", state="qualified")
    )
    assert _bundle(_with(_facts(), operations=operations))["sections"]["gates"]["release"] == {"state": "qualified"}


def test_the_worker_reason_becomes_a_protocol_note_only_without_a_worker_projection() -> None:
    base = _facts()
    with_reason = _with(
        base, protocol=ProtocolFacts(server_reason="server_not_configured", worker_reason="no_managed_worker")
    )
    notes = _bundle(with_reason)["sections"]["protocol"]["notes"]
    assert {
        "part": "managed_worker",
        "reason": "no_managed_worker",
        "detail": "no managed Review worker is configured for this server",
    } in notes
    projected = _with(
        base,
        protocol=ProtocolFacts(
            server_reason="server_not_configured",
            client_window=("2026-10-06", "2026-10-05"),
            managed_worker={"compatible": True, "negotiated_protocol": "2026-10-06"},
            worker_reason="worker_unreachable",
        ),
    )
    protocol = _bundle(projected)["sections"]["protocol"]
    assert protocol["managed_worker"]["compatible"] is True
    assert all(note["part"] != "managed_worker" for note in protocol["notes"])


def test_order_by_recency_compares_instants_not_spellings_and_breaks_ties_by_id() -> None:
    from lemoncrow.pro.capabilities.review.diagnostic_bundle import order_by_recency

    ranked = order_by_recency(
        [
            # 10:00Z written as +02:00 sorts after 11:00Z as a string, but is the earlier instant.
            ("older-by-instant", "2026-10-01T12:00:00+02:00"),
            ("newer-by-instant", "2026-10-01T11:00:00Z"),
            ("no-time", ""),
            ("tie-b", "2026-09-01T00:00:00Z"),
            ("tie-a", "2026-09-01T00:00:00.000000Z"),
            ("garbage", "not a time"),
        ]
    )
    assert ranked == ["newer-by-instant", "older-by-instant", "tie-a", "tie-b", "garbage", "no-time"]


# --------------------------------------------------------------------------- #
# RC10 fix round: marks, oracles, false positives, encodings, masks, names, files
# --------------------------------------------------------------------------- #

ROUND_SECRET = "Zq8vLk2pWm4rTx"


def _scrub(text: str, **environ: str) -> str:
    return scrub_included_text(text, environ=environ)


@pytest.mark.parametrize(
    "disguise",
    [
        lambda secret: "".join(char + "̀" for char in secret),
        lambda secret: "".join(char + "̣́" for char in secret),
        lambda secret: secret.replace("a", "à").replace("e", "é").replace("o", "ö").replace("u", "ú"),
        lambda secret: unicodedata.normalize("NFD", secret.replace("a", "à").replace("e", "é")),
        lambda secret: secret.replace("o", "ø").replace("l", "ł").replace("d", "đ"),
    ],
    ids=["combining-grave", "stacked-marks", "composed-accents", "decomposed-accents", "stroked-letters"],
)
def test_accents_and_combining_marks_do_not_hide_a_secret(disguise: Any) -> None:
    secret = "quartz-heron-47-adobe-fold-ulna"
    out = _scrub(f"failed with {disguise(secret)} now", APP_TOKEN=secret)
    assert out == "failed with <env:APP_TOKEN> now", out
    guard = SecretGuard.from_environ({"APP_TOKEN": secret})
    assert guard.overlaps(f"endpoint-{disguise(secret)}")
    assert match_key(disguise(secret))[0] == match_key(secret)[0]


def test_match_key_origins_stay_aligned_for_decomposing_characters() -> None:
    text = "aúﬁb"
    key, origins = match_key(text)
    assert key == "auf" + "ib"
    assert len(key) == len(origins) and origins == sorted(origins)


def test_percent_and_unicode_escapes_are_decoded_before_matching() -> None:
    secret = "quartz-heron-47-adobe"
    percent = "".join(f"%{ord(char):02X}" for char in secret)
    escaped = "".join(f"\\u{ord(char):04x}" for char in secret)
    doubled = percent.replace("%", "%25")
    for form in (percent, escaped, doubled, secret.replace("-", "%2D")):
        assert _scrub(f"failed {form} now", APP_TOKEN=secret) == "failed <env:APP_TOKEN> now", form


def test_cookie_values_are_removed() -> None:
    out = _scrub("GET failed\nCookie: session=abc123def456; theme=dark\nSet-Cookie: sid=Zq8vLk2pWm; Path=/ done")
    assert "abc123" not in out and "sid=Zq8" not in out and "theme" not in out
    assert out.count("<cookie>") == 2


@pytest.mark.parametrize("value", ["abc12", "k9X7q", "pw-1"])
def test_a_short_secret_named_value_at_the_floor_is_redacted(value: str) -> None:
    floor_met = len(value) >= 5
    out = _scrub(f"login failed with {value} for dave", DB_PASSWORD=value)
    assert (value not in out) is floor_met, out


def test_values_shorter_than_the_floor_are_not_secrets_and_the_floor_is_documented() -> None:
    from lemoncrow.pro.capabilities.review.diagnostic_secrets import MIN_SECRET_CHARS

    assert MIN_SECRET_CHARS == 5
    assert _scrub("state is ok now", APP_TOKEN="ok") == "state is ok now"


# ---- false positives -------------------------------------------------------- #

DB_URL = "postgresql://lemoncrow:Xy7pq@postgres/db?sslmode=require&application_name=lemoncrow-server"
FP_ENV = {
    "LEMONCROW_DATABASE_URL": DB_URL,
    "LEMONCROW_LLM_BACKEND": "anthropic",
    "LEMONCROW_DB_SSLMODE": "verify-full",
    "LANG": "en_US.UTF-8",
    "LEMONCROW_REQUIRE_AUTH": "enforced",
}


def test_only_the_url_password_and_secret_named_query_values_are_secrets() -> None:
    guard = SecretGuard.from_environ(FP_ENV)
    assert set(guard.labels) == {"xy7pq"}
    token_url = {"UPSTREAM": "https://bob:hunter22@h.example/x?sslmode=require&api_key=K9x7Pq2m&region=eu&sig=Aq83Lm"}
    assert set(SecretGuard.from_environ(token_url).labels) == {"hunter22", "k9x7pq2m", "aq83lm"}
    assert set(SecretGuard.from_environ({"U": "https://ghp1234567890abcdefXY@github.com/x"}).labels) == {
        "ghp1234567890abcdefxy"
    }
    assert not SecretGuard.from_environ({"U": "https://alice@github.com/x"}).labels


def test_non_secret_environment_values_stay_readable_in_error_text_and_names() -> None:
    out = scrub_included_text(
        "failed backend anthropic sslmode verify-full lang en_US.UTF-8 auth enforced require lemoncrow-server postgres",
        environ=FP_ENV,
    )
    for kept in ("anthropic", "verify-full", "enforced", "require", "lemoncrow-server", "postgres"):
        assert kept in out, out
    assert "Xy7pq" not in scrub_included_text(f"password {DB_URL.split(':')[2].split('@')[0]}", environ=FP_ENV)
    flow = _projection(
        _row("models.endpoint.main", "review policy run_plan.require"),
        _row("models.route.llm", "LEMONCROW_LLM_BACKEND"),
    )
    bundle = _bundle(_facts(flow=flow), FP_ENV)
    ids = {row["id"]: row for row in bundle["sections"]["data_flow"]["repositories"][0]["rows"]}
    assert set(ids) == {"models.endpoint.main", "models.route.llm"}
    settings = [origin["setting"] for row in ids.values() for origin in row["origins"]]
    assert settings == ["review policy run_plan.require", "LEMONCROW_LLM_BACKEND"]
    model = _model("anthropic/claude-sonnet-4", FP_ENV)
    assert model == "anthropic/claude-sonnet-4"


def test_the_setting_vocabulary_in_front_of_a_name_is_never_tested_only_the_operator_name() -> None:
    environ = {"LAB_API_KEY": "orchid-gateway-77"}
    flow = _projection(
        _row("models.endpoint.orchid-gateway-77", "providers.json endpoints.orchid-gateway-77"),
        _row("review.run_plan.require", "review policy run_plan.require"),
    )
    rows = _bundle(_facts(flow=flow), environ)["sections"]["data_flow"]["repositories"][0]["rows"]
    by_id = {row["id"]: row for row in rows}
    assert WITHHELD in by_id  # the operator-chosen name overlaps the secret
    assert by_id["review.run_plan.require"]["origins"][0]["setting"] == "review policy run_plan.require"
    assert "orchid" not in json.dumps(rows)
    # A secret that happens to equal the vocabulary in front of a name does not withhold the name.
    shown = _bundle(_facts(flow=flow), {"LAB_API_KEY": "review.run_plan"})["sections"]["data_flow"]["repositories"][0]
    assert "review.run_plan.require" in {row["id"] for row in shown["rows"]}


# ---- no guessing oracle for a tenant ---------------------------------------- #

SERVER_SECRET = "Zq8vLk2pWm4rTx9Bn"


def _tenant(text: str) -> str:
    guard = SecretGuard.from_environ({"SERVER_SECRET_KEY": SERVER_SECRET}, tenant_visible=True)
    return scrub_included_text(text, environ={}, guard=guard, env_names=False)


def test_a_tenant_supplied_fragment_of_a_server_secret_is_not_marked_differently_from_random_text() -> None:
    for fragment in (SERVER_SECRET[:6], SERVER_SECRET[3:9], SERVER_SECRET[8:], SERVER_SECRET[:7]):
        random_text = "Vh3nRb5Tc"[: len(fragment)].ljust(len(fragment), "k")
        probe = _tenant(f"request echoed {fragment} twice")
        control = _tenant(f"request echoed {random_text} twice")
        assert probe.replace(fragment, "X") == control.replace(random_text, "X"), (probe, control)
        assert "<env:" not in probe
    # An exact, whole value is still removed; and the local scrub still does fragments.
    assert _tenant(f"echoed {SERVER_SECRET} end") == "echoed <env:secret> end"
    local = scrub_included_text(f"echoed {SERVER_SECRET[:8]} end", environ={"SERVER_SECRET_KEY": SERVER_SECRET})
    assert SERVER_SECRET[:6] not in local


def test_a_tenant_cut_prefix_is_not_an_oracle_either() -> None:
    cap = 4 * MAX_INCLUDED_TEXT_CHARS
    prefix, control = SERVER_SECRET[:8], "Vh3nRb5T"
    outs = [_tenant(" " * (cap - 8) + part + " " * 600 + "tail") for part in (prefix, control)]
    assert len(outs[0]) == len(outs[1])


def test_names_answer_whole_values_and_long_overlaps_only_for_a_tenant() -> None:
    tenant = SecretGuard.from_environ({"SERVER_SECRET_KEY": SERVER_SECRET}, tenant_visible=True)
    local = SecretGuard.from_environ({"SERVER_SECRET_KEY": SERVER_SECRET})
    assert tenant.overlaps(SERVER_SECRET) and local.overlaps(SERVER_SECRET)
    assert tenant.overlaps(f"prod-{SERVER_SECRET}") and tenant.overlaps(f"{SERVER_SECRET[:10]}-gw")
    # three- and six-character probes: answered locally, never for a tenant
    for probe in (SERVER_SECRET[2:5], SERVER_SECRET[2:8]):
        assert local.overlaps(probe) and not tenant.overlaps(probe), probe


def test_a_name_that_partly_overlaps_a_secret_is_withheld() -> None:
    local = SecretGuard.from_environ({"APP_TOKEN": ROUND_SECRET})
    for name in ("prod-Zq8vLk2p", "Zq8vLk2pWm4rTx", "x-Zq8vLk-y", "prod-wm4rtx9"):
        assert local.overlaps(name) is (name != "prod-wm4rtx9" or len("wm4rtx") >= 6), name
    assert not local.overlaps("prod-gateway")
    flow = _projection(_row("models.endpoint.prod-Zq8vLk2p", "providers.json endpoints.prod-Zq8vLk2p"))
    bundle = _bundle(_facts(flow=flow), {"APP_TOKEN": ROUND_SECRET})
    assert "Zq8vLk" not in json.dumps(bundle)
    tenant = SecretGuard.from_environ({"APP_TOKEN": ROUND_SECRET}, tenant_visible=True)
    assert tenant.overlaps("prod-Zq8vLk2pWm") and not tenant.overlaps("prod-Zq8vLk")


# ---- the always-on message and degradation text ------------------------------ #

LEXICON_SECRET = "review-token-cache-lock"


def test_the_always_on_message_and_degradation_reasons_do_not_pass_a_lexicon_word_secret() -> None:
    job = IntelligenceJobRecord(
        job_id="job-1",
        kind="intelligence",
        review_id="rv-1",
        repo_id="repo-a",
        state="failed",
        created_at=NOW.isoformat(),
        error=f"auth failed for {LEXICON_SECRET} on {_fullwidth(LEXICON_SECRET)}",
    )
    from lemoncrow.pro.capabilities.review.diagnostic_bundle import SnapshotRecord

    snapshot = SnapshotRecord(
        review_id="rv-1",
        repo_id="repo-a",
        status="open",
        snapshot_id="snap-1",
        degraded=(f"cache lock {LEXICON_SECRET} held",),
    )
    base = _facts()
    facts = _with(
        base,
        records=ReviewRecords(
            snapshots=RecordSource((snapshot,)),
            intelligence_jobs=RecordSource((job,)),
            provider_operations=RecordSource(()),
            webhook_deliveries=RecordSource(unavailable="no_provider_webhooks"),
            repositories_scanned=1,
        ),
    )
    environ = {"LEMONCROW_DB_PASSWORD": LEXICON_SECRET}
    dumped = json.dumps(_bundle(facts, environ))
    assert "token-cache-lock" not in dumped and "token cache lock" not in dumped.lower()
    message = _bundle(facts, environ)["sections"]["recent_errors"]["records"][0]["message"]
    assert message.startswith("auth failed for") and "token" not in message and "lock" not in message
    # control: without the secret configured the lexicon words do pass (the redaction is not the cause)
    assert "token" in json.dumps(_bundle(facts, {}))


# ---- secrets that live in files -------------------------------------------- #


def test_providers_json_inline_keys_are_known_to_the_local_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.model_setup.endpoints import inline_api_keys

    root = tmp_path / "root"
    (root).mkdir()
    (root / "providers.json").write_text(
        json.dumps(
            {
                "custom": {
                    "endpoints": {
                        "lab": {"base_url": "http://x/v1", "api_key": "violet-harbor-lantern", "models": {"m": {}}},
                        "env": {
                            "base_url": "http://y/v1",
                            "api_key": "",
                            "api_key_env": "LAB_KEY",
                            "models": {"m": {}},
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert inline_api_keys(root) == ("violet-harbor-lantern",)
    assert inline_api_keys(tmp_path / "nowhere") == ()
    facts = _facts()
    job = IntelligenceJobRecord(
        job_id="job-1",
        kind="intelligence",
        review_id="rv-1",
        repo_id="repo-a",
        state="failed",
        created_at=NOW.isoformat(),
        model="violet-harbor-lantern",
        error="boom violet-harbor-lantern end",
    )
    facts = _with(
        facts,
        records=ReviewRecords(
            snapshots=RecordSource(()),
            intelligence_jobs=RecordSource((job,)),
            provider_operations=RecordSource(()),
            webhook_deliveries=RecordSource(unavailable="no_provider_webhooks"),
            repositories_scanned=1,
        ),
    )
    bundle = build_diagnostic_bundle(
        facts,
        include=LOCAL_INCLUDES,
        allowed_includes=LOCAL_INCLUDES,
        requested_by="t",
        now=NOW,
        salt=SALT,
        environ={},
        extra_secrets=inline_api_keys(root),
    )
    assert "harbor-lantern" not in json.dumps(bundle)
    leaky = build_diagnostic_bundle(
        facts, include=LOCAL_INCLUDES, allowed_includes=LOCAL_INCLUDES, requested_by="t", now=NOW, salt=SALT, environ={}
    )
    assert "harbor-lantern" in json.dumps(leaky)  # the control: without the file's key the model id shows


def test_the_cli_passes_providers_json_keys_to_the_builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review import diagnostic_bundle

    seen: dict[str, Any] = {}
    real = diagnostic_bundle.build_diagnostic_bundle

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen["extra"] = tuple(kwargs.get("extra_secrets", ()))
        return real(*args, **kwargs)

    monkeypatch.setattr(diagnostic_bundle, "build_diagnostic_bundle", spy)
    monkeypatch.setattr(diagnostic_local, "discover_server", lambda _config: (_ for _ in ()).throw(OSError("down")))
    root = tmp_path / "root"
    root.mkdir()
    (root / "providers.json").write_text(
        json.dumps(
            {
                "custom": {
                    "endpoints": {
                        "lab": {"base_url": "http://x/v1", "api_key": "violet-harbor-lantern", "models": {"m": {}}}
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["--root", str(root), "review", "diagnostics"], catch_exceptions=False)
    assert result.exit_code == 0, result.output[:300]
    assert seen["extra"] == ("violet-harbor-lantern",)


# ---- gate H state ------------------------------------------------------------ #


def test_gate_h_falling_back_to_defaults_is_unknown_not_a_populated_report_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lemoncrow.pro.capabilities.review import diagnostic_state, evidence_qualification

    monkeypatch.setattr(evidence_qualification, "packaged_contract_path", lambda: Path("/nonexistent-gate-dir/c.yaml"))
    evidence_qualification.runtime_gate_h_config.cache_clear()
    try:
        states = {state.gate: state for state in diagnostic_state.gate_states()}
    finally:
        evidence_qualification.runtime_gate_h_config.cache_clear()
    assert states["gate_h"].unreadable and states["gate_h"].mode == ""
    gates = _bundle(
        _with(
            _facts(),
            operations=__import__(
                "lemoncrow.pro.capabilities.review.diagnostic_bundle", fromlist=["OperationsFacts"]
            ).OperationsFacts(gates=tuple(states.values())),
        )
    )["sections"]["gates"]["gates"]
    gate_h = next(row for row in gates if row["gate"] == "gate_h")
    assert gate_h["status"] == "unknown" and gate_h["reason"] == "gate_unreadable"


def test_gate_h_is_read_fresh_not_from_the_process_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.review import diagnostic_state, evidence_qualification

    evidence_qualification.runtime_gate_h_config.cache_clear()
    primed = evidence_qualification.runtime_gate_h_config()  # the checkout's real file, now cached
    assert not primed.source.startswith("built-in defaults")
    assert next(state for state in diagnostic_state.gate_states() if state.gate == "gate_h").mode == primed.mode
    monkeypatch.setattr(evidence_qualification, "packaged_contract_path", lambda: Path("/nonexistent-gate-dir/c.yaml"))
    try:
        # The cache still answers the old state; the bundle must not.
        assert evidence_qualification.runtime_gate_h_config().source == primed.source
        assert next(state for state in diagnostic_state.gate_states() if state.gate == "gate_h").unreadable
    finally:
        evidence_qualification.runtime_gate_h_config.cache_clear()
