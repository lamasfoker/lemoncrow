"""Contract tests for slash-command parsing."""

from __future__ import annotations

from lemoncrow.gateway.cli.slash import parse_input


def test_slash_parser_contract() -> None:
    for text in ("", "   "):
        assert parse_input(text).kind == "empty"
    plain = parse_input("hello world")
    assert (plain.kind, plain.text) == ("message", "hello world")
    for text in ("/exit", "/quit"):
        assert parse_input(text).kind == "exit"
    assert parse_input("/clear").kind == "clear"

    cases = [
        ("/help", "help", []),
        ("/memory foo bar", "memory", ["foo", "bar"]),
        ("/background", "background", []),
        ("/unknown", "unknown", []),
    ]
    for text, name, args in cases:
        parsed = parse_input(text)
        assert (parsed.kind, parsed.name, parsed.args) == ("slash", name, args), text
