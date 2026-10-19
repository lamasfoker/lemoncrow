from __future__ import annotations

import json
import random
import string
import time

from lemoncrow_client.kit.redaction import (
    _redact_json_values,
    escape_jsonl_line_breaks,
    redact,
    redact_jsonl,
    redact_list,
)


def _parses(line: str) -> bool:
    try:
        json.loads(line)
    except ValueError:
        return False
    return True


def test_basic_redaction_contract() -> None:
    """case: test_redacts_openai_key"""
    assert "sk-" not in redact("token sk-ABCDEFGHIJKLMNOPQRSTUV1234567890")
    "case: test_redacts_credential_pair"
    assert "<redacted-credential>" in redact("api_key=supersecretthing123")
    "case: test_redacts_chain_of_thought_marker"
    out = redact("step 1 fine\nchain of thought: secret reasoning here")
    assert "<redacted-hidden-reasoning>" in out
    assert "<redacted-marker>" not in out
    assert "chain of thought" not in out
    assert "secret reasoning here" not in out
    "case: test_multiword_credential_value_is_fully_redacted"
    out = redact("authorization token: Bearer abc123SECRETvalue")
    assert "abc123SECRETvalue" not in out
    assert "Bearer" not in out
    assert "<redacted-credential>" in out
    "case: test_repeated_secret_is_redacted_globally"
    secret = "token=s3cr3tVALUE"
    out = redact(f"see {secret}\nand again {secret}\nend")
    assert "s3cr3tVALUE" not in out
    assert out.count("<redacted-credential>") == 2
    "case: test_credential_redaction_stays_on_its_own_line"
    out = redact("password: hunter2supersecret\nkeep_this_line")
    assert "hunter2supersecret" not in out
    assert "<redacted-credential>" in out
    assert "keep_this_line" in out
    "case: test_ordinary_identifier_is_not_over_redacted"
    out = redact("AWS_SECRET = 'AKIAIOSFODNN7EXAMPLE'")
    assert "AWS_SECRET" in out
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "<redacted-aws-key>" in out
    "case: test_ordinary_text_with_credential_keyword_is_not_redacted"
    prose = "the token economics of LLMs and the tokenizer: BPE design"
    assert redact(prose) == prose
    "case: test_redacts_jwt"
    jwt = "eyJABCDEFGHIJ.eyJABCDEFGHIJ.signaturepartXYZ"
    assert "<redacted-jwt>" in redact(f"Bearer {jwt}")
    "case: test_redacts_email"
    out = redact("ping me at pankaj4u4m@gmail.com please")
    assert "pankaj4u4m@gmail.com" not in out
    assert "<redacted-email>" in out
    "case: test_redact_list_applies_per_item"
    out = redact_list(["clean", "password=hunter2"])
    assert out[0] == "clean"
    assert "<redacted-credential>" in out[1]


def test_jsonl_redaction_contract() -> None:
    """case: test_redact_jsonl_keeps_every_record_parseable"""
    record = {"type": "message", "text": "from x import y\n\n@pytest.fixture\ntoken: hunter2", "id": "m1"}
    line = json.dumps(record)
    assert _parses(redact(line)) is False, "precondition: plain redact() corrupts this record"
    out = redact_jsonl(line)
    decoded = json.loads(out)
    assert decoded["type"] == "message" and decoded["id"] == "m1"
    assert "hunter2" not in out
    assert "<redacted-credential>" in out
    assert "from x import y" in decoded["text"]
    "case: test_redact_jsonl_candidate_scan_never_skips_a_real_match"
    for payload in [
        "nothing secret here at all",
        "sk-ABCDEFGHIJKLMNOPQRSTUV12345678",
        "ghp_ABCDEFGHIJKLMNOPQRSTUV12345678",
        "shppa_ABCDEFGHIJKLMNOPQRSTUV12345678",
        "user@example.com",
        "token: Bearer hunter2",
        "AKIA1234567890ABCDEF",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
        "-----BEGIN RSA PRIVATE KEY-----\\nMIIBOgIBAAJBAK\\n-----END RSA PRIVATE KEY-----",
        "<think>hidden</think>",
        "chain of thought: hidden",
        "internal reasoning: hidden",
    ]:
        text = "\n".join(
            [
                json.dumps({"type": "message", "text": payload}),
                json.dumps({"type": "noise", "text": "ordinary line"}),
                f"plain text {payload}",
            ]
        )
        assert redact_jsonl(text) == _reference_redact_jsonl(text)
    "case: test_redact_jsonl_escapes_unicode_line_separators"
    line = json.dumps({"text": "before\u2028after"}, ensure_ascii=False)
    assert len(line.splitlines()) == 2, "precondition: raw U+2028 splits the record"
    out = redact_jsonl(line)
    assert len(out.splitlines()) == 1
    assert json.loads(out)["text"] == "before\u2028after"
    "case: test_redact_jsonl_masks_secrets_inside_nested_values"
    line = json.dumps({"parts": [{"text": "key sk-ABCDEFGHIJKLMNOPQRSTUV12345678"}], "meta": {"user": "a@b.com"}})
    decoded = json.loads(redact_jsonl(line))
    assert decoded["parts"][0]["text"] == "key <redacted-openai-key>"
    assert decoded["meta"]["user"] == "<redacted-email>"
    "case: test_redact_jsonl_falls_back_to_text_redaction_off_json"
    text = "plain token: hunter2\n{not really json, token: hunter3"
    out = redact_jsonl(text)
    assert "hunter2" not in out and "hunter3" not in out
    assert out.count("<redacted-credential>") == 2


def test_block_redaction_contract() -> None:
    """case: test_redacts_private_key_body_of_any_length"""
    body = "A" * 200000
    out = redact(f"head -----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY----- tail")
    assert "AAAA" not in out
    assert "BEGIN RSA PRIVATE KEY" not in out
    assert "<redacted-private-key>" in out
    assert out.startswith("head ") and out.endswith(" tail")
    "case: test_redacts_reasoning_block_of_any_length"
    out = redact("pre <think>" + "secret-cot " * 20000 + "</think> post")
    assert "secret-cot" not in out
    assert "<redacted-hidden-reasoning>" in out
    assert out == "pre <redacted-hidden-reasoning> post"
    "case: test_unclosed_opener_does_not_hide_a_later_closed_block"
    out = redact("<think>orphan <thinking>REAL</thinking>")
    assert "REAL" not in out
    assert out == "<think>orphan <redacted-hidden-reasoning>"
    "case: test_missing_closing_literal_does_not_suppress_real_matches"
    noise = ("<think>" + "x" * 25) * 20000
    text = f"{noise}<think>the hidden part</think>tail"
    out = redact(text)
    assert "the hidden part" not in out
    assert "<redacted-hidden-reasoning>" in out
    key = "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAK\n-----END RSA PRIVATE KEY-----"
    key_out = redact(f"prefix\n{key}\nsuffix")
    assert "MIIBOgIBAAJBAK" not in key_out
    assert "<redacted-private-key>" in key_out
    assert "suffix" in key_out


def test_redact_does_not_hang_on_large_binary_blob() -> None:
    random.seed(3)
    alphabet = string.ascii_letters + string.digits + "_-"
    blob = "data:image/png;base64," + "".join(random.choice(alphabet) for _ in range(1000000))
    start = time.monotonic()
    redact(blob)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"redact() took {elapsed:.2f}s on a 1M-char blob -- catastrophic backtracking regression"


def test_redact_is_fast_on_densely_repeated_unclosed_think_tags() -> None:
    blob = ("<think>" + "x" * 25) * 62500
    start = time.monotonic()
    out = redact(blob)
    elapsed = time.monotonic() - start
    assert out == blob, "nothing to redact -- no closing </think> anywhere"
    assert elapsed < 2.0, f"redact() took {elapsed:.2f}s on 2MB of unclosed <think> openers"


def test_redact_is_fast_on_densely_repeated_unclosed_private_key_headers() -> None:
    blob = ("-----BEGIN A PRIVATE KEY-----" + "xxx") * 62500
    start = time.monotonic()
    out = redact(blob)
    elapsed = time.monotonic() - start
    assert out == blob, "nothing to redact -- no -----END ... PRIVATE KEY----- anywhere"
    assert elapsed < 2.0, f"redact() took {elapsed:.2f}s on 2MB of unclosed PEM headers"


def _reference_redact_jsonl(text: str) -> str:
    """redact_jsonl without its candidate-scan fast path -- the slow but
    obviously-correct version the optimized one must agree with."""
    out = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith(("{", "[")):
            try:
                decoded = json.loads(stripped)
            except ValueError:
                out.append(escape_jsonl_line_breaks(redact(line)))
                continue
            out.append(escape_jsonl_line_breaks(json.dumps(_redact_json_values(decoded), ensure_ascii=False)))
        else:
            out.append(escape_jsonl_line_breaks(redact(line)))
    return "\n".join(out)
