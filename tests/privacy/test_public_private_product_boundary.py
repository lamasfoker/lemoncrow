from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PUBLIC_PYTHON_ROOTS = (REPO / "src" / "lemoncrow", REPO / "server" / "src", REPO / "client" / "src")
PUBLIC_FRONTEND = REPO / "frontend" / "src"
KNOWN_PRIVATE_PATH_DEBT: set[str] = set()
PRIVATE_MODEL_NAMES = {
    "TeamWorkspace",
    "TeamInvite",
    "ReviewParticipant",
    "ReviewRequest",
    "ReviewAttention",
    "ReviewAttentionEvent",
    "ReviewProviderSubject",
    "ProviderWebhookReceipt",
    "ProviderOperation",
}
KNOWN_PRIVATE_MODEL_DEBT: set[str] = set()
PRIVATE_TABLES = {
    "review_participants",
    "review_requests",
    "review_attention",
    "review_attention_events",
    "review_provider_subjects",
    "provider_webhook_receipts",
    "provider_operations",
}
KNOWN_PRIVATE_TABLE_DEBT: set[str] = set()
PRIVATE_ROUTE_PREFIXES = ("/v1/team/", "/v1/governance/policy", "/v1/audit/export", "/v1/audit/verify")
KNOWN_PRIVATE_ROUTE_DEBT: set[str] = set()
HOSTED_FRONTEND_NAMES = {
    "HostedReviewContext",
    "HostedReviewParticipant",
    "HostedReviewRequest",
    "HostedReviewProviderSubject",
    "HostedReviewProviderOperation",
}
KNOWN_HOSTED_FRONTEND_DEBT: set[str] = set()


def _relative(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def _public_python_files() -> list[Path]:
    return [path for root in PUBLIC_PYTHON_ROOTS for path in root.rglob("*.py") if ".venv" not in path.parts]


def test_public_private_runtime_contract() -> None:
    """case: test_public_runtime_never_imports_enterprise_implementation"""
    offenders: list[str] = []
    for path in _public_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                if (
                    name == "lemoncrow_server"
                    or name.startswith("lemoncrow_server.")
                    or name == "enterprise"
                    or name.startswith("enterprise.")
                ):
                    offenders.append(f"{_relative(path)}:{node.lineno} -> {name}")
    assert offenders == [], "public runtime imports private implementation: " + ", ".join(offenders)
    "case: test_public_runtime_never_names_private_repository_paths"
    "Public implementation must not reveal or depend on private repo layout."
    forbidden = ("docs-internal/", "enterprise/server", "enterprise/frontend", "enterprise/deploy")
    offenders: list[str] = []
    roots = (*PUBLIC_PYTHON_ROOTS, PUBLIC_FRONTEND)
    for root in roots:
        if not root.exists():
            continue
        for pattern in ("*.py", "*.ts", "*.tsx"):
            for path in root.rglob(pattern):
                if "__pycache__" in path.parts or ".venv" in path.parts:
                    continue
                text = path.read_text(encoding="utf-8")
                for token in forbidden:
                    if token in text:
                        offenders.append(f"{_relative(path)} -> {token}")
    assert offenders == [], "private repository layout leaked into public runtime: " + ", ".join(offenders)
    "case: test_public_server_core_has_no_multi_principal_access_policy"
    "Hosted sharing/admin reach decisions belong to enterprise composition."
    public_server = REPO / "server" / "src" / "lemoncrow_server_core"
    forbidden = ("cross_subject", "shares_repository", "authorizer.scoped", "sharing_grant", "subject:act_as")
    offenders: list[str] = []
    for path in public_server.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                offenders.append(f"{_relative(path)} -> {token}")
    assert offenders == [], "hosted multi-principal policy leaked into public server core: " + ", ".join(offenders)
    "case: test_public_cli_has_no_organization_admin_commands"
    admin = (REPO / "src/lemoncrow/gateway/cli/commands/admin.py").read_text(encoding="utf-8")
    memory = (REPO / "src/lemoncrow/gateway/cli/commands/memory.py").read_text(encoding="utf-8")
    forbidden = (
        '@click.group("team")',
        '@click.group("governance")',
        '@audit_group.command("export")',
        '@audit_group.command("verify")',
    )
    assert all(token not in admin for token in forbidden)
    assert '@memory_group_cli.command("share")' not in memory
    "case: test_public_frontend_never_imports_enterprise"
    offenders: list[str] = []
    if PUBLIC_FRONTEND.exists():
        for path in (*PUBLIC_FRONTEND.rglob("*.ts"), *PUBLIC_FRONTEND.rglob("*.tsx")):
            text = path.read_text(encoding="utf-8")
            if "enterprise/" in text or "@enterprise/" in text:
                offenders.append(_relative(path))
    assert offenders == [], f"public frontend imports private implementation: {offenders}"
    "case: test_public_license_does_not_claim_private_source"
    root_license = (REPO / "LICENSE").read_text(encoding="utf-8")
    assert "licensed in its entirety" not in root_license
    assert "enterprise/" in root_license
    assert "not granted under this Apache-2.0 license" in root_license


def test_private_debt_contract() -> None:
    """case: test_private_product_path_debt_does_not_expand"""
    current: set[str] = set()
    for root in (
        REPO / "src/lemoncrow/pro/capabilities/team",
        REPO / "src/lemoncrow/core/capabilities/governance",
        REPO / "src/lemoncrow/core/capabilities/audit_export",
    ):
        if root.exists():
            current.update(_relative(path) for path in root.rglob("*.py") if "__pycache__" not in path.parts)
    contract = REPO / "src/lemoncrow/core/capabilities/team_contract.py"
    if contract.exists():
        current.add(_relative(contract))
    assert (
        current <= KNOWN_PRIVATE_PATH_DEBT
    ), f"new private-product implementation leaked into public paths: {sorted(current - KNOWN_PRIVATE_PATH_DEBT)}"
    "case: test_private_domain_model_debt_does_not_expand"
    current: set[str] = set()
    for path in _public_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name in PRIVATE_MODEL_NAMES:
                current.add(f"{_relative(path)}:{node.name}")
    assert (
        current <= KNOWN_PRIVATE_MODEL_DEBT
    ), f"new private collaboration model leaked into public source: {sorted(current - KNOWN_PRIVATE_MODEL_DEBT)}"
    "case: test_private_review_table_debt_does_not_expand"
    current: set[str] = set()
    for root in (REPO / "src/lemoncrow", REPO / "server/src"):
        for pattern in ("*.py", "*.sql"):
            for path in root.rglob(pattern):
                if "__pycache__" in path.parts:
                    continue
                for table in _CREATE_TABLE.findall(path.read_text(encoding="utf-8")):
                    if table in PRIVATE_TABLES:
                        current.add(f"{_relative(path)}:{table}")
    assert (
        current <= KNOWN_PRIVATE_TABLE_DEBT
    ), f"new private Review table leaked into public source: {sorted(current - KNOWN_PRIVATE_TABLE_DEBT)}"
    "case: test_private_service_route_debt_does_not_expand"
    current: set[str] = set()
    route_decorator = re.compile("@app\\.(?:get|post|put|patch|delete)\\(\\s*[\\\"\\']([^\\\"\\']+)")
    for path in _public_python_files():
        text = path.read_text(encoding="utf-8")
        routes = route_decorator.findall(text)
        if any(any(route.startswith(prefix) for prefix in PRIVATE_ROUTE_PREFIXES) for route in routes):
            current.add(_relative(path))
    assert (
        current <= KNOWN_PRIVATE_ROUTE_DEBT
    ), f"new organization/service route leaked into public source: {sorted(current - KNOWN_PRIVATE_ROUTE_DEBT)}"
    "case: test_hosted_frontend_type_debt_does_not_expand"
    current: set[str] = set()
    if PUBLIC_FRONTEND.exists():
        for path in (*PUBLIC_FRONTEND.rglob("*.ts"), *PUBLIC_FRONTEND.rglob("*.tsx")):
            text = path.read_text(encoding="utf-8")
            if any(name in text for name in HOSTED_FRONTEND_NAMES):
                current.add(_relative(path))
    assert (
        current <= KNOWN_HOSTED_FRONTEND_DEBT
    ), f"new hosted collaboration knowledge leaked into public frontend: {sorted(current - KNOWN_HOSTED_FRONTEND_DEBT)}"


def test_retired_account_surface_contract() -> None:
    """case: test_retired_public_account_licensing_runtime_is_absent"""
    licensing_dir = REPO / "src/lemoncrow/core/capabilities/licensing"
    assert not any(licensing_dir.glob("*.py")) if licensing_dir.exists() else True
    assert not (REPO / "src/lemoncrow/pro/capabilities/licensing_gate.py").exists()
    assert not (REPO / "scripts/test_pro.sh").exists()
    "case: test_public_runtime_has_no_retired_account_licensing_imports"
    forbidden_modules = ("lemoncrow.core.capabilities.licensing", "lemoncrow.pro.capabilities.licensing_gate")
    offenders: list[str] = []
    for path in _public_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                if any(name == forbidden or name.startswith(f"{forbidden}.") for forbidden in forbidden_modules):
                    offenders.append(f"{_relative(path)}:{node.lineno} -> {name}")
    assert offenders == [], "retired account/licensing import leaked into public runtime: " + ", ".join(offenders)
    "case: test_public_runtime_has_no_account_cap_protocol_vocabulary"
    forbidden = (
        "capVerdictToken",
        "monthlySavingsCapInUsd",
        "savingsOverCap",
        "LEMONCROW_AUTH_TOKEN",
        "LEMONCROW_DEVICE_ID",
        "lc account login",
    )
    offenders: list[str] = []
    for path in _public_python_files():
        if path.name == "legacy_migration.py":
            continue
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                offenders.append(f"{_relative(path)} -> {token}")
    assert offenders == [], "retired commercial protocol vocabulary leaked into public runtime: " + ", ".join(offenders)


_CREATE_TABLE = re.compile("CREATE\\s+TABLE\\s+IF\\s+NOT\\s+EXISTS\\s+([A-Za-z0-9_]+)", re.IGNORECASE)
