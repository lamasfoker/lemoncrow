"""Unit tests for edit_impact: substitution-based rename detection + removed-symbol extraction."""

from __future__ import annotations

from lemoncrow.pro.capabilities.tool_supervision.edit_impact import (
    _call_patterns,
    _call_queries,
    _def_signatures,
    _is_call_occurrence,
    _is_identifier_occurrence,
    _method_uncertainty,
    _removed_module_symbols,
    _signature_change_params,
    literal_replacements,
    removed_literals,
)


def test_literal_rename_contract() -> None:
    """case: test_detects_genuine_single_line_rename"""
    edits = [{"old_string": "config['db'] = value", "new_string": "config['database'] = value"}]
    assert literal_replacements(edits) == {"db": "database"}
    "case: test_multiline_rename_detected_after_line_shift"
    edits = [
        {
            "old_string": "import old_helper\nrow = fetch('passwd')\nreturn row",
            "new_string": "row = fetch('password')\nlog(row)\nreturn row",
        }
    ]
    assert literal_replacements(edits).get("passwd") == "password"
    "case: test_no_phantom_rename_from_positional_misalignment"
    edits = [
        {
            "old_string": 'import est\nd["skeleton"] = True\nd["tokens_saved"] = s',
            "new_string": 'd["skeleton"] = True\nd["tokens_saved"] = s\nreturn None',
        }
    ]
    result = literal_replacements(edits)
    assert "skeleton" not in result
    assert "tokens_saved" not in result
    assert removed_literals(edits) == []
    "case: test_removed_literal_without_replacement_maps_to_none"
    edits = [{"old_string": "x = {'legacy_key': 1, 'keep': 2}", "new_string": "x = {'keep': 2}"}]
    assert literal_replacements(edits) == {"legacy_key": None}


def test_removed_symbol_contract() -> None:
    """case: test_removed_module_symbols_flags_renamed_def_and_const"""
    edits = [
        {"old_string": "def compute_total(x):\n    return x", "new_string": "def compute_sum(x):\n    return x"},
        {"old_string": "MAX_RETRIES = 3", "new_string": "MAX_ATTEMPTS = 3"},
    ]
    assert set(_removed_module_symbols(edits)) == {"compute_total", "MAX_RETRIES"}
    "case: test_removed_module_symbols_ignores_indented_and_unchanged"
    edits = [
        {
            "old_string": "class Widget:\n    def render(self):\n        pass",
            "new_string": "class Widget:\n    def draw(self):\n        pass",
        }
    ]
    assert _removed_module_symbols(edits) == []
    "case: test_is_identifier_occurrence_gate"
    assert _is_identifier_occurrence("return DEFAULT_TIMEOUT", "DEFAULT_TIMEOUT")
    assert _is_identifier_occurrence("from p.c import DEFAULT_TIMEOUT", "DEFAULT_TIMEOUT")
    assert not _is_identifier_occurrence("# DEFAULT_TIMEOUT is gone", "DEFAULT_TIMEOUT")
    assert not _is_identifier_occurrence("x = NEW_DEFAULT_TIMEOUT", "DEFAULT_TIMEOUT")
    assert not _is_identifier_occurrence("obj.DEFAULT_TIMEOUT", "DEFAULT_TIMEOUT")


def test_signature_change_contract() -> None:
    """case: test_signature_change_flags_new_required_param"""
    edits = [
        {"old_string": "def render(node):\n    return node", "new_string": "def render(node, theme):\n    return node"}
    ]
    assert _signature_change_params(edits) == {"render": ["theme"]}
    "case: test_signature_change_ignores_new_optional_param"
    edits = [{"old_string": "def render(node):\n    ...", "new_string": "def render(node, theme=None):\n    ..."}]
    assert _signature_change_params(edits) == {}
    "case: test_signature_change_flags_lost_default"
    edits = [
        {"old_string": "def render(node, theme=None):\n    ...", "new_string": "def render(node, theme):\n    ..."}
    ]
    assert _signature_change_params(edits) == {"render": ["theme"]}
    "case: test_signature_change_ignores_annotation_commas_and_self"
    edits = [
        {
            "old_string": "def build(self, opts: dict[str, int]):\n    ...",
            "new_string": "def build(self, opts: dict[str, int], sink: Sink):\n    ...",
        }
    ]
    assert _signature_change_params(edits) == {"build": ["sink"]}
    "case: test_def_signatures_qualifies_methods_by_class"
    "Indentation is the only class signal an edit hunk carries; it is enough."
    text = "class SessionManager:\n    def refresh(self, user):\n        pass\n\n    class Inner:\n        def nested(self, a):\n            pass\n\n\ndef helper(x):\n    pass\n"
    keys = _def_signatures(text)
    assert set(keys) == {"SessionManager.refresh", "Inner.nested", "helper"}
    assert keys["SessionManager.refresh"] == "self, user"
    "case: test_signature_change_keys_a_method_by_its_class"
    edits = [
        {
            "old_string": "class SessionManager:\n    def refresh(self, user):\n        pass",
            "new_string": "class SessionManager:\n    def refresh(self, user, context):\n        pass",
        }
    ]
    assert _signature_change_params(edits) == {"SessionManager.refresh": ["context"]}
    "case: test_a_method_moved_between_classes_is_not_a_signature_change"
    "Old and new are matched on the qualified key, so a move is a move.\n\n    Matching on the bare name would report ``refresh`` as having gained ``context``\n    on whichever class happened to be compared -- a change no reviewer could verify.\n    "
    edits = [
        {
            "old_string": "class A:\n    def refresh(self, user):\n        pass",
            "new_string": "class B:\n    def refresh(self, user, context):\n        pass",
        }
    ]
    assert _signature_change_params(edits) == {}


def test_call_detection_contract() -> None:
    """case: test_call_detection_matches_the_shape_each_kind_of_def_is_actually_called_by"""
    "A method is reached as ``obj.name(``; a module-level function never is."
    assert _call_patterns("SessionManager.refresh") == ["$OBJ.refresh($$$)"]
    assert _call_patterns("refresh_session") == ["refresh_session($$$)"]
    assert _call_queries("SessionManager.refresh") == [".refresh("]
    assert _call_queries("refresh_session") == ["refresh_session"]
    method = "SessionManager.refresh"
    assert _is_call_occurrence("return manager.refresh(user)", method)
    assert _is_call_occurrence("self.refresh(user)", method)
    assert not _is_call_occurrence("# manager.refresh(user)", method)
    assert not _is_call_occurrence("return refresh(user)", method)
    assert not _is_call_occurrence("return manager.refresh_all(user)", method)
    module_level = "refresh_session"
    assert _is_call_occurrence("return refresh_session(token)", module_level)
    assert not _is_call_occurrence("return api.refresh_session(token)", module_level)
    "case: test_method_uncertainty_states_the_doubt_and_never_invents_certainty"
    "One definition is proof of ownership; anything else, or no answer, is not."
    assert _method_uncertainty("SessionManager", "refresh", 1) == ""
    ambiguous = _method_uncertainty("SessionManager", "refresh", 3)
    assert "at least 3 definitions named refresh()" in ambiguous
    assert "SessionManager.refresh" in ambiguous
    unknown = _method_uncertainty("SessionManager", "refresh", None)
    assert unknown and "could not count" in unknown
