# LemonCrow Python SDK

The Python SDK is an embeddable interface for custom integrations, tests, and service-side code that need LemonCrow without driving the CLI.

The stable facade is `LemonCrowClient` in `lemoncrow.gateway.sdk` (re-exported by the SDK package where installed).

## Source-checkout setup

```bash
uv sync --all-extras
```

## Client modes

```python
from lemoncrow.gateway.sdk import LemonCrowClient

local = LemonCrowClient.local()
remote = LemonCrowClient.remote(base_url="https://your-lemoncrow.example", api_key="...")
mcp = LemonCrowClient.mcp()
```

- `local()` uses the in-process local store/runtime contract.
- `remote()` targets a configured LemonCrow HTTP service.
- `mcp()` mirrors the low-level protocol contract for integration/testing use.

Normal coding-agent users do not need the Python SDK or MCP client; use the host plugin/integration instead.

## Core facade operations

The current facade exposes operations such as:

```python
context = local.get_context(
    task="Find the parser path responsible for this failing test",
    files=["src/parser.py"],
)

rescue = local.rescue_failure(
    task="Fix the parser regression",
    error="focused test still fails",
)

result = local.run_rubric_gate(
    rubric_id="example-rubric",
    checks={"focused_test_passed": True},
)

trace = local.record_trace(
    agent="sdk",
    domain="code.change",
    task="Fix the parser regression",
    status="success",
)
```

Exact DTO fields and return types live in the SDK source and type annotations; prefer those definitions over copied examples when building a long-lived integration.

## Namespaced clients

`LemonCrowClient` also exposes namespaced helpers for the public contracts implemented by the selected backend, including:

- `playbooks` / `blocks`
- `rubrics`
- `traces`
- `evals`
- `savings`
- `memory`
- `lessons`

Availability can differ between local, remote, and protocol-backed clients. A namespace existing on the facade does not imply every commercial/private capability is present in an OSS-only install.

## Low-level protocol client

`LemonCrowClient.mcp()` is for integration authors and tests. The model-facing core tool allowlist is documented in [Protocol / MCP reference](mcp.md) and currently centers on:

- `bash`
- `code_search`
- `edit`
- `read`
- `web_fetch`

Do not hard-code a broader historical tool list into a new integration. Tool visibility is deny-by-default and product/trust profiles can narrow it further.

## CLI equivalents

Operational workflows remain CLI-first. Current examples include:

```bash
lc rt benchmark --help
lc rt service --help
lc rt worker --help
lc status
```

For coding-agent setup, use the [installation guide](../setup/installation.md) rather than embedding the SDK directly.
