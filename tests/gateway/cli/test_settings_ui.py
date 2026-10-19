"""`lc settings`: the picker's model, its rendering, the terminal shell, and the plain commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from lemoncrow.core import settings as settings_module
from lemoncrow.core.reply_register import REPLY_REGISTER_LEVELS, TELEGRAPHIC_SETTING_KEY
from lemoncrow.core.settings import all_settings, find_by_key, load_raw
from lemoncrow.gateway.cli import cli
from lemoncrow.gateway.cli.picker import Picker, Row, cells, run
from lemoncrow.gateway.cli.settings_ui import (
    APPROVAL_KEYS,
    SettingsSource,
    SettingsStore,
    display_value,
    require,
    search,
    settings_picker,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No exported LEMONCROW_* knob from the developer's shell may leak into these screens."""
    for spec in all_settings():
        if spec.env_var and spec.settable:
            monkeypatch.delenv(spec.env_var, raising=False)
    monkeypatch.setattr(settings_module, "_SEEDED_ENV", set())
    monkeypatch.setenv("NO_COLOR", "1")


@pytest.fixture
def store(tmp_path: Path) -> SettingsStore:
    return SettingsStore(tmp_path)


# ── store ──


def test_store_reports_defaults_changes_and_resets(store: SettingsStore, tmp_path: Path) -> None:
    assert store.get("attribution").value is True
    assert store.get("attribution").flag == ""

    changed = store.set("attribution", "off")
    assert (changed.value, changed.flag, changed.shown) == (False, "changed", "off")
    assert load_raw(tmp_path)["attribution"] is False

    back = store.reset("attribution")
    assert (back.value, back.flag) == (True, "")
    assert "attribution" not in load_raw(tmp_path)


def test_exported_env_var_is_flagged_only_when_it_wins_with_a_different_value(
    store: SettingsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_OFFLINE", "1")
    current = store.get("cli.offline")
    assert (current.value, current.flag, current.env_shown) == (True, "env", "LEMONCROW_OFFLINE=1")

    store.set("cli.offline", "on")  # same value as the environment: nothing to explain
    assert store.get("cli.offline").flag == "changed"

    # A value this process seeded from the settings file is not the user's export.
    monkeypatch.setattr(settings_module, "_SEEDED_ENV", {"LEMONCROW_OFFLINE"})
    monkeypatch.setenv("LEMONCROW_OFFLINE", "0")
    assert store.get("cli.offline").env is None


def test_read_only_settings_read_from_the_environment_without_an_override_warning(
    store: SettingsStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    current = store.get("core.root")
    assert (current.value, current.env) == (str(tmp_path), None)
    with pytest.raises(ValueError, match="read-only"):
        store.reset("core.root")


def test_unknown_keys_suggest_the_nearest_setting() -> None:
    with pytest.raises(ValueError, match=r"unknown setting: cli\.telegrafic\. Did you mean cli\.telegraphic\?"):
        require("cli.telegrafic")
    with pytest.raises(ValueError, match=r"^unknown setting: zzz$"):
        require("zzz")


def test_reply_style_choices_match_the_reply_register_levels() -> None:
    spec = find_by_key(TELEGRAPHIC_SETTING_KEY)
    assert spec is not None and spec.choices == REPLY_REGISTER_LEVELS


def test_values_read_as_words_and_secrets_stay_hidden() -> None:
    def show(key: str, value: object) -> str:
        spec = find_by_key(key)
        assert spec is not None
        return display_value(spec, value)

    assert show("attribution", True) == "on"
    assert show("review.fix_agent", "") == "none"
    assert show("llm.openai_base_url", "") == "not set"
    assert show("llm.openai_api_key", "sk-live-123") == "hidden"
    assert show("service.gateway_token", "tok") == "hidden"
    assert show("tool_supervision.owned_max_tokens", 512) == "512"


def test_search_puts_key_matches_first_and_lists_everything_when_empty() -> None:
    keys = [spec.key for spec in search("memory")]
    assert keys[0].startswith("memory.")
    assert "review.preview_memory_mb" in keys
    assert len(search("")) == len(all_settings())
    assert search("no-such-word-anywhere") == []


# ── picker ──


def _select(picker: Picker, label: str) -> None:
    for index, row in enumerate(picker.page.rows):
        if row.label == label:
            picker.page.cursor = index
            return
    raise AssertionError(f"no row {label!r} in {[r.label for r in picker.page.rows]}")


def _selected(picker: Picker) -> Row:
    row = picker.selected()
    assert row is not None
    return row


def test_home_lists_the_essentials_and_skips_headings(store: SettingsStore) -> None:
    picker = settings_picker(store)
    screen = picker.text(100, 40)
    assert screen.splitlines()[0] == "◆ LemonCrow settings"
    for label in ("Reply style", "Commit attribution", "Approval gates", "Browse all settings", "Search settings"):
        assert label in screen
    assert _selected(picker).label == "Reply style"
    picker.move(-1)
    assert _selected(picker).label == "Reply style"
    picker.move(100)
    assert _selected(picker).label == "Search settings"


def test_a_switch_flips_and_saves_on_enter(store: SettingsStore, tmp_path: Path) -> None:
    picker = settings_picker(store)
    _select(picker, "Commit attribution")
    picker.activate()
    assert load_raw(tmp_path)["attribution"] is False
    assert picker.message == ("ok", "Commit attribution set to off")
    assert _source(picker).changes == {"attribution": "off"}
    assert "• off" in picker.text()


def _source(picker: Picker) -> SettingsSource:
    assert isinstance(picker.source, SettingsSource)
    return picker.source


def test_a_choice_opens_a_list_on_the_current_value(store: SettingsStore, tmp_path: Path) -> None:
    picker = settings_picker(store)
    picker.activate()  # Reply style
    assert picker.page.title == "Reply style"
    assert [row.label for row in picker.page.rows] == list(REPLY_REGISTER_LEVELS)
    assert _selected(picker).ref == ("choice", TELEGRAPHIC_SETTING_KEY, "ultra")
    assert "current · default" in picker.text()
    assert "⏎ choose" in picker.text()
    picker.move(1)
    picker.activate()
    assert load_raw(tmp_path)[TELEGRAPHIC_SETTING_KEY] == "lite"
    assert len(picker.stack) == 1
    assert _selected(picker).value == "lite"


def test_an_empty_default_choice_offers_none(store: SettingsStore) -> None:
    picker = settings_picker(store)
    _select(picker, "Review fix agent")
    picker.activate()
    assert picker.page.rows[0].label == "none"
    assert _selected(picker).ref[2] == ""


def test_typed_values_are_validated_and_kept_open_on_error(store: SettingsStore, tmp_path: Path) -> None:
    picker = settings_picker(store)
    picker.start_search()
    picker.set_query("preview_memory_mb")
    picker.activate()
    assert picker.mode == "edit"
    assert picker.editing is not None and picker.editing.initial == "4096"
    assert picker.submit("lots") is False
    assert picker.mode == "edit"
    assert picker.message is not None and picker.message[0] == "fail"
    assert picker.submit(" 2048 ") is True
    assert load_raw(tmp_path)["review.preview_memory_mb"] == 2048
    assert picker.mode == "search"
    assert _selected(picker).value == "2048"


def test_secrets_start_empty_and_never_show(store: SettingsStore) -> None:
    store.set("llm.openai_api_key", "sk-live-123")
    picker = settings_picker(store)
    picker.start_search()
    picker.set_query("llm.openai_api_key")
    assert "sk-live" not in picker.text()
    picker.activate()
    assert picker.editing is not None
    assert (picker.editing.initial, picker.editing.secret) == ("", True)


def test_reset_and_read_only(store: SettingsStore, tmp_path: Path) -> None:
    store.set("attribution", "off")
    picker = settings_picker(store)
    _select(picker, "Commit attribution")
    assert picker.press("r") is True
    assert "attribution" not in load_raw(tmp_path)
    assert picker.message == ("ok", "Commit attribution reset to on")

    picker.start_search()
    picker.set_query("core.root")
    picker.activate()
    assert picker.mode == "search"
    assert picker.message is not None and picker.message[0] == "warn"
    assert "core.root" not in load_raw(tmp_path)


def test_approval_gates_summarise_and_drop_their_shared_boilerplate(store: SettingsStore) -> None:
    store.set("review.approval.git_push", "on")
    picker = settings_picker(store)
    _select(picker, "Approval gates")
    assert (_selected(picker).value, _selected(picker).flag) == ("git push", "changed")
    picker.activate()
    assert [row.ref[1] for row in picker.page.rows] == list(APPROVAL_KEYS)
    notes = {row.label: row.note for row in picker.page.rows}
    assert notes["git_push"] == "Git push or force-push."
    assert notes["wait_seconds"].startswith("How long the agent waits")
    # The footer still carries the full description of the selected gate.
    assert "Pause the coding agent" in picker.text()


def test_search_narrows_and_escape_steps_back_out(store: SettingsStore) -> None:
    picker = settings_picker(store)
    picker.start_search()
    assert picker.mode == "search"
    assert len(picker.page.rows) == len(all_settings())
    picker.set_query("letta")
    assert {row.ref[1] for row in picker.page.rows} >= {"memory.letta_url", "memory.letta_api_key"}
    picker.set_query("no-such-word-anywhere")
    assert "No setting matches" in picker.text()
    picker.back()
    assert picker.mode == "browse" and len(picker.stack) == 1
    picker.back()
    assert picker.done


def test_areas_list_every_category_with_change_counts(store: SettingsStore) -> None:
    store.set("attribution", "off")
    picker = settings_picker(store)
    _select(picker, "Browse all settings")
    picker.activate()
    rows = {row.label: row for row in picker.page.rows}
    assert sum(len(row.ref[1]) for row in rows.values()) == len(all_settings())
    assert (rows["plugin"].value, rows["plugin"].note) == ("9 settings", "1 changed")
    _select(picker, "retrieval")
    picker.activate()
    assert picker.page.title == "retrieval"
    assert not any(row.label.startswith("retrieval.") for row in picker.page.rows)


@pytest.mark.parametrize("width", [120, 80, 60, 40])
def test_every_line_fits_the_terminal(store: SettingsStore, width: int) -> None:
    picker = settings_picker(store)
    home = picker.text(width, 30)
    picker.start_search()
    picker.set_query("openmemory")
    for screen in (home, picker.text(width, 30)):
        assert max(cells(line) for line in screen.splitlines()) <= width, screen


def test_long_lists_scroll_with_the_cursor_and_say_where_you_are(store: SettingsStore) -> None:
    picker = settings_picker(store)
    picker.start_search()
    picker.move(50)
    screen = picker.text(100, 10)
    selected = [line for line in screen.splitlines() if line.startswith("  \u276f ")]
    assert len(selected) == 1
    assert f"51 of {len(all_settings())}" in screen


# ── the terminal shell ──


def test_picker_runs_on_real_key_presses(store: SettingsStore, tmp_path: Path) -> None:
    picker = settings_picker(store)
    with create_pipe_input() as keys:
        keys.send_text("j\r")  # down to Commit attribution, flip it
        keys.send_text("/preview_memory\r")  # search, open the first hit for editing
        keys.send_text("\x7f" * 8 + "2048\r")
        keys.send_text("\x1b")  # leave the search
        keys.send_text("q")
        with create_app_session(input=keys, output=DummyOutput()):
            run(picker)
    assert load_raw(tmp_path) == {"attribution": False, "review.preview_memory_mb": 2048}
    assert _source(picker).changes == {"attribution": "off", "review.preview_memory_mb": "2048"}


# ── commands ──


def _lc(root: Path, *args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli, ["--root", str(root), "settings", *args], prog_name="lc")
    return result.exit_code, result.output


def test_settings_off_a_terminal_prints_the_overview(tmp_path: Path) -> None:
    _lc(tmp_path, "set", "retrieval.ann_cache_limit", "7")
    code, out = _lc(tmp_path)
    assert code == 0, out
    assert out.startswith("◆ LemonCrow settings")
    assert "Essentials" in out and "cli.telegraphic" in out
    assert "Also changed" in out and "retrieval.ann_cache_limit  • 7" in out
    assert "• changed from default" in out
    assert "lc settings show --all" in out
    assert _lc(tmp_path, "show")[1] == out


def test_show_all_and_category_list_every_setting(tmp_path: Path) -> None:
    code, out = _lc(tmp_path, "show", "--all")
    assert code == 0, out
    assert all(spec.key in out for spec in all_settings())
    code, out = _lc(tmp_path, "show", "--category", "plugin")
    assert "plugin · 9 settings" in out and "retrieval." not in out
    code, out = _lc(tmp_path, "show", "--category", "nope")
    assert code == 1 and "unknown category: nope" in out


def test_show_json_keeps_its_shape(tmp_path: Path) -> None:
    _lc(tmp_path, "set", "attribution", "off")
    payload = json.loads(_lc(tmp_path, "show", "--json")[1])
    assert payload["attribution"] is False
    assert set(payload) == {spec.key for spec in all_settings()}


def test_get_explains_one_setting(tmp_path: Path) -> None:
    _lc(tmp_path, "set", TELEGRAPHIC_SETTING_KEY, "lite")
    code, out = _lc(tmp_path, "get", TELEGRAPHIC_SETTING_KEY)
    assert code == 0, out
    assert out.startswith("◆ cli.telegraphic  Reply style")
    assert "Value     lite  (default ultra)" in out
    assert "Choices   ultra · lite · off" in out
    assert "lc settings reset cli.telegraphic" in out
    payload = json.loads(_lc(tmp_path, "get", TELEGRAPHIC_SETTING_KEY, "--json")[1])
    assert payload["value"] == "lite" and payload["changed"] is True and payload["default"] == "ultra"


def test_typos_get_a_suggestion(tmp_path: Path) -> None:
    for args in (("get", "atribution"), ("set", "atribution", "off"), ("reset", "atribution")):
        code, out = _lc(tmp_path, *args)
        assert code == 1
        assert "unknown setting: atribution. Did you mean attribution?" in out


def test_set_and_reset_confirm_what_happened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _lc(tmp_path, "set", "attribution", "off")[1] == "✓ attribution set to off\n"
    assert _lc(tmp_path, "reset", "attribution")[1] == "✓ attribution reset to on\n"
    assert _lc(tmp_path, "reset", "attribution")[1] == "○ attribution is already at its default (on)\n"

    monkeypatch.setenv("LEMONCROW_OFFLINE", "1")
    out = _lc(tmp_path, "set", "cli.offline", "off")[1]
    assert "LEMONCROW_OFFLINE=1 in your environment still overrides it" in out


def test_reply_style_rejects_unknown_levels(tmp_path: Path) -> None:
    code, out = _lc(tmp_path, "set", TELEGRAPHIC_SETTING_KEY, "")
    assert code == 1 and "choices: ultra, lite, off" in out
