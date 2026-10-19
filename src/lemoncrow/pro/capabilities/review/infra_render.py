"""Render infrastructure and deploy config (Phase 9R item 33, ``infra.render``).

* helm charts render with ``helm template`` and kustomizations with
  ``kustomize build`` (or ``kubectl kustomize``) on each side; the host parses
  the rendered documents into resources (Secret values masked) and diffs them.
* Terraform root modules get a credential-free ``terraform plan``: a local
  state backend in the sandbox's ``/tmp`` is forced through an override file,
  providers come only from a trusted-base filesystem mirror (an
  ``execution.fixtures`` path) or are built in, declared AWS providers get mock
  credentials with every account/credential lookup skipped, ``-refresh=false``
  keeps the plan offline, and every credential variable is absent from the
  tool's environment. An estimated monthly cloud-cost delta is derived from a
  static price table and always labelled an estimate.

**Never apply.** The terraform command builder is a closed table of the
``version``/``init``/``plan``/``show`` steps; the specs accept no command,
argument or flag, and both the host builder and the in-sandbox driver refuse
any forbidden subcommand or flag (``apply``, ``destroy``, ``-auto-approve`` ...).
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any, Final

import yaml

from lemoncrow.pro.capabilities.review.analysis_common import (
    INFRA_ENTRY,
    MAX_TIMEOUT,
    AnalysisSpec,
    Evaluation,
    Finding,
    Observation,
    SideRun,
    bounded,
    build_driver,
    first_line,
    relative_path,
    sha256_hex,
    valid_spec_id,
)

TOOLS: Final[tuple[str, ...]] = ("terraform", "helm", "kustomize")
#: The only terraform invocations that exist (``-chdir`` and ``-var-file`` are added per spec).
TERRAFORM_STEPS: Final[Mapping[str, tuple[str, ...]]] = {
    "version": ("version", "-json"),
    "init": ("init", "-input=false", "-no-color", "-lock=false", "-backend=true", "-get=true"),
    "plan": ("plan", "-input=false", "-no-color", "-lock=false", "-refresh=false", "-out=<plan>"),
    "show": ("show", "-json", "-no-color", "<plan>"),
}
TERRAFORM_ORDER: Final[tuple[str, ...]] = ("version", "init", "plan", "show")
FORBIDDEN_TERRAFORM: Final[frozenset[str]] = frozenset(
    {
        "apply",
        "destroy",
        "import",
        "state",
        "taint",
        "untaint",
        "force-unlock",
        "workspace",
        "console",
        "login",
        "logout",
        "refresh",
        "test",
        "-auto-approve",
        "-destroy",
    }
)
#: Spec keys that would let config inject a command; refused outright.
_COMMAND_KEYS: Final[frozenset[str]] = frozenset(
    {"command", "commands", "args", "argv", "flags", "options", "steps", "subcommand", "extra_args", "env", "script"}
)
_ALLOWED_KEYS: Final[frozenset[str]] = frozenset(
    {"id", "title", "tool", "dir", "var_files", "provider_mirror", "values", "release", "namespace", "timeout"}
)
_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]{0,51}[a-z0-9])?$")
_MAX_DISCOVERED: Final[int] = 8
_MAX_RESOURCE_FINDINGS: Final[int] = 30
_MAX_RENDERED_CHARS: Final[int] = 2_000_000
_MASK: Final[str] = "<masked>"
_HINTS: Final[Mapping[str, str]] = {
    "terraform": (
        "Install terraform in the executor image; providers must come from a trusted-base filesystem mirror "
        "(analyses.infra[].provider_mirror naming an execution.fixtures path) because the sandbox has no network."
    ),
    "helm": "Install helm in the executor image and vendor chart dependencies under charts/ (no network in the sandbox).",
    "kustomize": "Install kustomize (or kubectl) in the executor image; remote bases cannot be fetched offline.",
}
_COST_BASIS: Final[str] = "static on-demand list prices, us-east-1 or equivalent, 730 h/month; an estimate, not a bill"
#: USD per month by resource type and sizing attribute (rounded list prices). An estimate only.
_PRICES: Final[Mapping[tuple[str, str], Mapping[str, float]]] = {
    ("aws_instance", "instance_type"): {
        "t3.nano": 3.8,
        "t3.micro": 7.6,
        "t3.small": 15.2,
        "t3.medium": 30.4,
        "t3.large": 60.7,
        "t3.xlarge": 121.5,
        "m5.large": 70.1,
        "m5.xlarge": 140.2,
        "m5.2xlarge": 280.3,
        "c5.large": 62.1,
        "c5.xlarge": 124.1,
        "r5.large": 92.0,
        "r5.xlarge": 184.0,
    },
    ("aws_db_instance", "instance_class"): {
        "db.t3.micro": 12.4,
        "db.t3.small": 24.8,
        "db.t3.medium": 49.6,
        "db.m5.large": 124.8,
        "db.m5.xlarge": 249.6,
        "db.r5.large": 175.2,
    },
    ("google_compute_instance", "machine_type"): {
        "e2-micro": 6.1,
        "e2-small": 12.2,
        "e2-medium": 24.5,
        "e2-standard-2": 48.9,
        "n1-standard-1": 24.3,
        "n2-standard-2": 70.9,
    },
    ("azurerm_linux_virtual_machine", "size"): {
        "Standard_B1s": 7.6,
        "Standard_B2s": 30.4,
        "Standard_D2s_v3": 70.1,
        "Standard_D4s_v3": 140.2,
    },
}
_FLAT_PRICES: Final[Mapping[str, float]] = {
    "aws_nat_gateway": 32.9,
    "aws_lb": 16.4,
    "aws_alb": 16.4,
    "aws_elb": 18.3,
    "aws_eip": 3.6,
    "aws_eks_cluster": 73.0,
    "google_container_cluster": 73.0,
    "azurerm_kubernetes_cluster": 73.0,
    "aws_kms_key": 1.0,
    "aws_secretsmanager_secret": 0.4,
}
COST_KEYS: Final[tuple[str, ...]] = ("instance_type", "instance_class", "machine_type", "size", "vm_size")

DRIVER: Final[str] = build_driver(r'''
PLAN = WORK + "/plan.bin"
TF_STEPS = {
    "version": ("version", "-json"),
    "init": ("init", "-input=false", "-no-color", "-lock=false", "-backend=true", "-get=true"),
    "plan": ("plan", "-input=false", "-no-color", "-lock=false", "-refresh=false", "-out=" + PLAN),
    "show": ("show", "-json", "-no-color", PLAN),
}
FORBIDDEN = frozenset(("apply", "destroy", "import", "state", "taint", "untaint", "force-unlock", "workspace",
                       "console", "login", "logout", "refresh", "test", "-auto-approve", "-destroy"))
COST_KEYS = ("instance_type", "instance_class", "machine_type", "size", "vm_size")
TFENV = {}
AWS_MOCK = """
provider "aws" {
  region                      = "us-east-1"
  access_key                  = "mock-access-key"
  secret_key                  = "mock-secret-key"
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
}
"""


def safe(value):
    value = str(value)
    if not value or value.startswith("-") or value.startswith("/") or ".." in value.split("/"):
        finish(3, "refused an unsafe path argument")
    return value


def tf(step, wd, extra=()):
    # The closed step table is the only way terraform is ever started.
    if step not in TF_STEPS:
        finish(3, "refused terraform step %s" % step)
    argv = ["terraform", "-chdir=" + wd] + list(TF_STEPS[step]) + list(extra)
    if any(arg.split("=", 1)[0] in FORBIDDEN for arg in argv[1:]):
        finish(3, "refused a forbidden terraform argument")
    # Every executed command line is reported (without the work directory).
    R.setdefault("steps", []).append([step] + argv[2:])
    return run(argv, wd, extra=TFENV, split=True)


def first(msg):
    for line in msg.splitlines():
        line = line.strip().lstrip("│").strip()
        if line and not line.startswith("╷") and not line.startswith("╵"):
            return line[:300]
    return ""


def terraform():
    if which("terraform") is None:
        finish(2, "terraform is not installed in the sandbox")
    src = copy_root()
    wd = os.path.normpath(os.path.join(src, safe(P.get("dir") or ".") if P.get("dir") not in (None, "", ".") else "."))
    if not os.path.isdir(wd):
        R["missing"] = True
        finish(0)
    for name in os.listdir(wd):
        if name.endswith((".tfstate", ".tfstate.backup")) or name in (".terraform", ".terraform.lock.hcl"):
            path = os.path.join(wd, name)
            shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.unlink(path)
    body = "".join(text(os.path.join(wd, n)) or "" for n in sorted(os.listdir(wd)) if n.endswith(".tf"))
    blocks = re.findall(r'(?ms)^\s*provider\s+"aws"\s*\{(.*?)^\s*\}', body)
    override = 'terraform {\n  backend "local" {\n    path = "%s/state/terraform.tfstate"\n  }\n}\n' % WORK
    if blocks and any("alias" not in block for block in blocks):
        override += AWS_MOCK
        R["mock_providers"] = ["aws"]
    os.makedirs(WORK + "/state", exist_ok=True)
    with open(os.path.join(wd, "zz_lemoncrow_override.tf"), "w") as handle:
        handle.write(override)
    mirror = str(P.get("mirror") or "")
    if mirror and not os.path.isdir(mirror):
        finish(2, "the trusted-base provider mirror %s is not mounted in this sandbox" % mirror)
    os.makedirs(WORK + "/providers", exist_ok=True)
    config = WORK + "/terraformrc"
    with open(config, "w") as handle:
        handle.write('disable_checkpoint = true\nprovider_installation {\n  filesystem_mirror {\n    path = "%s"\n  }\n}\n'
                     % (mirror or WORK + "/providers"))
    TFENV.update({"TF_CLI_CONFIG_FILE": config, "TF_DATA_DIR": WORK + "/tfdata", "TF_IN_AUTOMATION": "1",
                  "TF_INPUT": "0", "CHECKPOINT_DISABLE": "1"})
    code, out, err = tf("version", wd)
    try:
        R["tool_version"] = json.loads(out).get("terraform_version")
    except ValueError:
        R["tool_version"] = first(out)
    code, out, err = tf("init", wd)
    if code != 0:
        msg = err or out
        R["error"] = msg[-4000:]
        if re.search(r"not found in any of the search locations|Failed to query available provider|Failed to install provider"
                     r"|Failed to download module|Could not download module|no available releases", msg, re.I):
            finish(2, "terraform providers or modules are not available offline: " + first(msg))
        finish(4, "terraform init failed: " + first(msg))
    extra = []
    for name in P.get("var_files") or []:
        path = os.path.join(wd, safe(name))
        if not os.path.isfile(path):
            finish(4, "the var file %s is missing on this side" % name)
        extra.append("-var-file=" + path)
    code, out, err = tf("plan", wd, extra)
    R["heavy"]["plan.txt"] = out[-300000:]
    if code != 0:
        msg = err or out
        R["error"] = msg[-4000:]
        if re.search(r"No valid credential sources|could not find default credentials|InvalidClientTokenId"
                     r"|error configuring .* provider|credentials", msg, re.I):
            finish(2, "the provider needs credentials, which the sandbox never provides: " + first(msg))
        finish(4, "terraform plan failed: " + first(msg))
    code, out, err = tf("show", wd)
    if code != 0:
        finish(4, "terraform show failed: " + first(err or out))
    plan = json.loads(out)
    changes = []
    for row in plan.get("resource_changes") or []:
        change = row.get("change") or {}
        after = change.get("after") if isinstance(change.get("after"), dict) else {}
        digest = hashlib.sha256(json.dumps([after, change.get("after_unknown"), change.get("after_sensitive")],
                                           sort_keys=True, default=str).encode()).hexdigest()[:16]
        changes.append({"address": row.get("address"), "type": row.get("type"), "mode": row.get("mode"),
                        "provider": row.get("provider_name"), "actions": change.get("actions") or [], "digest": digest,
                        "attrs": {k: after[k] for k in COST_KEYS if isinstance(after.get(k), (str, int, float, bool))}})
    outputs = {k: (v or {}).get("actions") for k, v in (plan.get("output_changes") or {}).items()}
    R["plan"] = {"resource_changes": changes[:800], "count": len(changes), "outputs": outputs,
                 "terraform_version": plan.get("terraform_version")}
    finish(0)


def render(argv, src, extra=None):
    code, out, err = run(argv, src, extra=extra, split=True)
    if code is None:
        finish(2, err)
    if code != 0:
        R["error"] = (err or out)[-4000:]
        if re.search(r"missing in charts/ directory|dependencies are missing|failed to download|no such host", err, re.I):
            finish(2, "chart dependencies or remote bases are not available offline: " + first(err))
        finish(4, "rendering failed: " + first(err or out))
    R["heavy"]["rendered.yaml"] = out
    finish(0)


def helm():
    if which("helm") is None:
        finish(2, "helm is not installed in the sandbox")
    src = copy_root()
    chart = os.path.normpath(os.path.join(src, safe(P.get("dir")) if P.get("dir") not in (None, "", ".") else "."))
    if not os.path.isdir(chart):
        R["missing"] = True
        finish(0)
    argv = ["helm", "template", str(P.get("release") or "release"), chart, "--namespace", str(P.get("namespace") or "default")]
    for name in P.get("values") or []:
        argv += ["--values", os.path.join(chart, safe(name))]
    home = WORK + "/helm"
    render(argv, src, {"HELM_CACHE_HOME": home + "/cache", "HELM_CONFIG_HOME": home + "/config", "HELM_DATA_HOME": home + "/data"})


def kustomize():
    src = copy_root()
    target = os.path.normpath(os.path.join(src, safe(P.get("dir")) if P.get("dir") not in (None, "", ".") else "."))
    if not os.path.isdir(target):
        R["missing"] = True
        finish(0)
    if which("kustomize"):
        render(["kustomize", "build", target], src)
    if which("kubectl"):
        render(["kubectl", "kustomize", target], src)
    finish(2, "kustomize is not installed in the sandbox (kustomize or kubectl)")


def main():
    tool = P.get("tool")
    if tool == "terraform":
        terraform()
    elif tool == "helm":
        helm()
    elif tool == "kustomize":
        kustomize()
    finish(3, "unsupported infrastructure tool %s" % tool)
''')


# --------------------------------------------------------------------------- #
# command builder (host side): a closed table, never config
# --------------------------------------------------------------------------- #


def terraform_argv(step: str, *, workdir: str, plan_file: str, var_files: Sequence[str] = ()) -> tuple[str, ...]:
    """The one terraform invocation of *step*; anything outside the table raises."""

    if step not in TERRAFORM_STEPS:
        raise ValueError(f"terraform step {step!r} is not allowed; only {', '.join(TERRAFORM_ORDER)} exist")
    argv = ["terraform", f"-chdir={workdir}", *(arg.replace("<plan>", plan_file) for arg in TERRAFORM_STEPS[step])]
    if step == "plan":
        for name in var_files:
            if relative_path(name, allow_root=False) is None:
                raise ValueError(f"var file {name!r} is not a repository-relative path")
            argv.append(f"-var-file={workdir}/{name}")
    if any(arg.split("=", 1)[0] in FORBIDDEN_TERRAFORM for arg in argv[1:]):
        raise ValueError("refused a forbidden terraform argument")
    return tuple(argv)


def terraform_plan_commands(spec: AnalysisSpec, *, workdir: str, plan_file: str) -> tuple[tuple[str, ...], ...]:
    """Every terraform command one infra spec runs, in order."""

    var_files = [str(item) for item in spec.options.get("var_files") or ()]
    return tuple(
        terraform_argv(step, workdir=workdir, plan_file=plan_file, var_files=var_files) for step in TERRAFORM_ORDER
    )


# --------------------------------------------------------------------------- #
# trusted-base specs
# --------------------------------------------------------------------------- #


def _spec(spec_id: str, tool: str, directory: str, *, origin: str, source: str, **options: Any) -> AnalysisSpec:
    title = str(options.pop("title", "") or "")
    timeout = int(options.pop("timeout", 0) or 300)
    mirror = str(options.get("provider_mirror") or "")
    label = {"terraform": "Terraform plan", "helm": "Helm render", "kustomize": "Kustomize render"}[tool]
    return AnalysisSpec(
        entry_id=INFRA_ENTRY,
        id=spec_id,
        tool=tool,
        root=".",
        title=title or f"{label} · {directory}",
        origin=origin,  # type: ignore[arg-type]
        source=source,
        options={"dir": directory, **options},
        timeout=timeout,
        setup_hint=_HINTS[tool],
        trusted_inputs=(f"fixture:{mirror}",) if mirror else (),
    )


def parse_specs(rows: Sequence[Any], errors: list[str]) -> list[AnalysisSpec]:
    """``analyses.infra`` rows: structured fields only; any command-shaped key is refused."""

    specs: list[AnalysisSpec] = []
    for index, row in enumerate(rows):
        where = f"analyses.infra[{index}]"
        if not isinstance(row, Mapping):
            errors.append(f"{where} must be a mapping")
            continue
        commands = sorted(str(key) for key in row if key in _COMMAND_KEYS)
        if commands:
            errors.append(
                f"{where}: infra analyses take no commands, arguments or flags ({', '.join(commands)}); "
                "LemonCrow only renders and plans, it never applies"
            )
            continue
        unknown = sorted(str(key) for key in set(row) - _ALLOWED_KEYS)
        if unknown:
            errors.append(f"{where} has unknown keys: {', '.join(unknown)}")
            continue
        spec_id, tool = row.get("id"), str(row.get("tool") or "")
        directory = relative_path(row.get("dir"))
        lists: dict[str, list[str | None]] = {}
        for key in ("var_files", "values"):
            raw = row.get(key) or []
            lists[key] = [relative_path(item, allow_root=False) for item in raw] if isinstance(raw, list) else [None]
        mirror = relative_path(row.get("provider_mirror"), allow_root=False) if row.get("provider_mirror") else ""
        release, namespace = str(row.get("release") or "release"), str(row.get("namespace") or "default")
        try:
            timeout = max(30, min(int(row.get("timeout") or 300), MAX_TIMEOUT))
        except (TypeError, ValueError):
            errors.append(f"{where}.timeout must be a number of seconds")
            continue
        if not valid_spec_id(spec_id):
            errors.append(f"{where}.id must match [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}")
        elif tool not in TOOLS:
            errors.append(f"{where}.tool must be one of {', '.join(TOOLS)}")
        elif directory is None:
            errors.append(f"{where}.dir must be a repository-relative directory")
        elif any(item is None for values in lists.values() for item in values):
            errors.append(f"{where}: var_files and values must be relative file paths (no flags)")
        elif mirror is None:
            errors.append(f"{where}.provider_mirror must be a relative execution.fixtures path")
        elif not _NAME.match(release) or not _NAME.match(namespace):
            errors.append(f"{where}: release and namespace must be DNS labels")
        elif tool != "terraform" and (lists["var_files"] or mirror):
            errors.append(f"{where}: var_files and provider_mirror apply to terraform only")
        elif tool != "helm" and (lists["values"] or row.get("release") or row.get("namespace")):
            errors.append(f"{where}: values, release and namespace apply to helm only")
        else:
            options: dict[str, Any] = {"title": str(row.get("title") or "")[:200], "timeout": timeout}
            if tool == "terraform":
                options.update(var_files=[str(item) for item in lists["var_files"]], provider_mirror=mirror)
            if tool == "helm":
                options.update(values=[str(item) for item in lists["values"]], release=release, namespace=namespace)
            specs.append(_spec(str(spec_id), tool, directory, origin="configured", source=where, **options))
    return specs


def discover_specs(paths: Sequence[str], read_base: Callable[[str], str | None]) -> list[AnalysisSpec]:
    """Terraform root modules, helm charts and kustomizations in the trusted base."""

    found: dict[tuple[str, str], str] = {}
    terraform_dirs: dict[str, list[str]] = {}
    for path in sorted(paths):
        parts = PurePosixPath(path).parts
        if any(
            part.startswith(".") or part in {"node_modules", "vendor", "examples", "testdata"} for part in parts[:-1]
        ):
            continue
        name = parts[-1]
        parent = str(PurePosixPath(path).parent)
        if name.endswith(".tf") and "modules" not in parts[:-1]:
            terraform_dirs.setdefault(parent, []).append(path)
        elif name in {"Chart.yaml", "Chart.yml"}:
            found.setdefault(("helm", parent), path)
        elif name in {"kustomization.yaml", "kustomization.yml", "Kustomization"}:
            found.setdefault(("kustomize", parent), path)
    reads = 0
    for directory, files in sorted(terraform_dirs.items()):
        for path in files:
            if reads >= 64:
                break
            reads += 1
            text = read_base(path) or ""
            if re.search(r'(?m)^\s*(terraform\s*\{|provider\s+"|resource\s+"|module\s+")', text):
                found.setdefault(("terraform", directory), path)
                break
    specs: list[AnalysisSpec] = []
    seen: set[str] = set()
    charts = {directory for tool, directory in found if tool == "helm"}
    for (tool, directory), source in sorted(
        found.items(), key=lambda item: (item[0][1].count("/"), item[0][1], item[0][0])
    ):
        if len(specs) >= _MAX_DISCOVERED:
            break
        if tool == "kustomize" and any(
            directory.startswith(chart.rstrip("/") + "/") for chart in charts if chart != "."
        ):
            continue
        base_id = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{tool}-{directory}" if directory != "." else tool).strip("-.")[:60]
        spec_id = base_id
        while spec_id in seen:
            spec_id = f"{base_id}-{len(seen)}"
        seen.add(spec_id)
        release = re.sub(r"[^a-z0-9-]+", "-", PurePosixPath(directory).name.lower()).strip("-")[:50] or "release"
        extra: dict[str, Any] = {"values": [], "release": release, "namespace": "default"} if tool == "helm" else {}
        if tool == "terraform":
            extra = {"var_files": [], "provider_mirror": ""}
        specs.append(_spec(spec_id, tool, directory, origin="discovered", source=source, **extra))
    return specs


def payload(spec: AnalysisSpec) -> dict[str, Any]:
    out: dict[str, Any] = {"tool": spec.tool, "dir": spec.options.get("dir") or "."}
    if spec.tool == "terraform":
        mirror = str(spec.options.get("provider_mirror") or "")
        out.update(var_files=list(spec.options.get("var_files") or ()), mirror=f"/fixtures/{mirror}" if mirror else "")
    elif spec.tool == "helm":
        out.update(
            values=list(spec.options.get("values") or ()),
            release=spec.options.get("release") or "release",
            namespace=spec.options.get("namespace") or "default",
        )
    return out


# --------------------------------------------------------------------------- #
# rendered resources and the cost estimate
# --------------------------------------------------------------------------- #


def _mask(document: Any) -> Any:
    if isinstance(document, Mapping) and str(document.get("kind") or "") == "Secret":
        masked = dict(document)
        for key in ("data", "stringData"):
            if isinstance(masked.get(key), Mapping):
                masked[key] = {str(name): _MASK for name in masked[key]}
        return masked
    return document


def rendered_resources(text: str) -> tuple[dict[str, Any], str, str]:
    """(resource identity -> masked document, masked YAML text, parse problem)."""

    if len(text) > _MAX_RENDERED_CHARS:
        return {}, "", f"rendered output exceeds {_MAX_RENDERED_CHARS} characters"
    try:
        documents = [doc for doc in yaml.safe_load_all(text) if doc is not None]
    except yaml.YAMLError as exc:
        # Never ``str(exc)``: its context snippet quotes the offending line (a Secret value).
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        problem = str(getattr(exc, "problem", "") or "unparseable")[:120]
        return {}, "", f"rendered output is not valid YAML{where}: {problem}"
    resources: dict[str, Any] = {}
    chunks: list[str] = []
    for index, document in enumerate(documents[:2000]):
        masked = _mask(document)
        metadata = (
            masked.get("metadata")
            if isinstance(masked, Mapping) and isinstance(masked.get("metadata"), Mapping)
            else {}
        )
        assert isinstance(metadata, Mapping)
        namespace = f"{metadata.get('namespace')}/" if metadata.get("namespace") else ""
        identity = (
            f"{masked.get('apiVersion', '')}/{masked.get('kind', '')}/{namespace}{metadata.get('name') or index}"
            if isinstance(masked, Mapping)
            else f"document/{index}"
        )
        resources[identity] = masked
        chunks.append(f"# {identity}\n" + yaml.safe_dump(masked, sort_keys=True, default_flow_style=False))
    return resources, "---\n".join(chunks), ""


def monthly_price(change: Mapping[str, Any]) -> float | None:
    kind = str(change.get("type") or "")
    attrs = change.get("attrs") if isinstance(change.get("attrs"), Mapping) else {}
    assert isinstance(attrs, Mapping)
    if kind in _FLAT_PRICES:
        return _FLAT_PRICES[kind]
    for (resource, attribute), table in _PRICES.items():
        if resource == kind:
            return table.get(str(attrs.get(attribute) or ""))
    return None


def estimate_cost(base: Sequence[Mapping[str, Any]], head: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Estimated monthly USD delta between two plans (both against empty state). Always an estimate."""

    def total(rows: Sequence[Mapping[str, Any]]) -> tuple[float, int, set[str]]:
        amount, priced, unpriced = 0.0, 0, set()
        for row in rows:
            if str(row.get("mode") or "managed") != "managed" or "delete" in (row.get("actions") or ()):
                continue
            price = monthly_price(row)
            if price is None:
                unpriced.add(str(row.get("type") or "?"))
            else:
                amount, priced = amount + price, priced + 1
        return amount, priced, unpriced

    base_total, base_priced, base_unpriced = total(base)
    head_total, head_priced, head_unpriced = total(head)
    return {
        "label": "estimate",
        "currency": "USD",
        "monthly_base": round(base_total, 2),
        "monthly_head": round(head_total, 2),
        "monthly_delta": round(head_total - base_total, 2),
        "priced": head_priced + base_priced,
        "unpriced_types": sorted(base_unpriced | head_unpriced)[:30],
        "basis": _COST_BASIS,
    }


def _changes(run: SideRun | None) -> dict[str, Mapping[str, Any]]:
    if run is None or not run.ran:
        return {}
    plan = run.report.get("plan") if isinstance(run.report.get("plan"), Mapping) else {}
    assert isinstance(plan, Mapping)
    return {
        str(row.get("address")): row
        for row in plan.get("resource_changes") or ()
        if isinstance(row, Mapping) and row.get("address")
    }


def _resource_findings(
    spec: AnalysisSpec,
    added: Sequence[str],
    removed: Sequence[str],
    changed: Sequence[str],
    *,
    noun: str,
    describe: Callable[[str, str], str],
) -> list[Finding]:
    findings: list[Finding] = []
    rows = (
        [("removed", name) for name in removed]
        + [("changed", name) for name in changed]
        + [("added", name) for name in added]
    )
    for change, name in rows[:_MAX_RESOURCE_FINDINGS]:
        findings.append(
            Finding(
                rule=f"resource_{change}",
                category="render" if spec.tool != "terraform" else "plan",
                subject=f"{spec.id}::{name}",
                title=f"{noun} {change} · {name}"[:200],
                summary=describe(change, name),
                severity="high" if change == "removed" else "medium" if change == "changed" else "low",
                change=change,  # type: ignore[arg-type]
                facts=(("Tool", spec.tool), ("Resource", name), ("Change", change)),
                base=Observation("present", name, sha256_hex([name, "base"])) if change != "added" else None,
                head=(
                    Observation("present", name, sha256_hex([name, change]))
                    if change != "removed"
                    else Observation("absent", name, "")
                ),
            )
        )
    if len(rows) > _MAX_RESOURCE_FINDINGS:
        extra = len(rows) - _MAX_RESOURCE_FINDINGS
        findings.append(
            Finding(
                rule="resource_more",
                category="render",
                subject=f"{spec.id}::more",
                title=f"{extra} more {noun.lower()} change(s) · {spec.title or spec.id}",
                summary="See the rendered diff artifact for the remaining changes.",
                severity="low",
                head=Observation("changed", str(extra), sha256_hex(rows[_MAX_RESOURCE_FINDINGS:])),
            )
        )
    return findings


def _unavailable(spec: AnalysisSpec, run: SideRun | None) -> Evaluation:
    return Evaluation(
        "NOT_RUN",
        [run.reason if run is not None and run.reason else f"{spec.tool} did not run"],
        setup_hint=_HINTS.get(spec.tool, ""),
    )


def evaluate(spec: AnalysisSpec, head: SideRun | None, base: SideRun | None) -> Evaluation:
    if head is None or head.state in {"not_run", "unavailable"}:
        return _unavailable(spec, head)
    if head.state == "error":
        return Evaluation("UNKNOWN", [head.reason or "the infrastructure driver returned no usable result"])
    if head.state == "failed":
        error = first_line(head.report.get("error")) or head.reason
        if base is not None and base.state == "failed":
            return Evaluation(
                "NOT_RUN",
                [
                    f"{spec.tool} fails on the trusted base as well: {first_line(base.report.get('error')) or base.reason}"
                ],
            )
        return Evaluation(
            "FAIL",
            [f"{spec.tool} fails on the Change: {error}"],
            [
                Finding(
                    rule="render_failed" if spec.tool != "terraform" else "plan_failed",
                    category="failed",
                    subject=f"{spec.id}::failed",
                    title=f"{'Plan' if spec.tool == 'terraform' else 'Render'} fails · {spec.title or spec.id}"[:200],
                    summary=bounded(error),
                    severity="high",
                    critical=True,
                    base=Observation(base.state, base.reason or "ok", "") if base is not None else None,
                    head=Observation("failed", bounded(error, 200), sha256_hex(error)),
                )
            ],
        )
    # Only a base that rendered/planned is a comparison; a failed base render is not an empty one.
    compared = base is not None and base.state in {"ok", "findings"}
    evaluation = (
        _evaluate_plan(spec, head, base if compared else None)
        if spec.tool == "terraform"
        else _evaluate_render(spec, head, base if compared else None)
    )
    if base is not None and not compared:
        evaluation.reasons.append(f"base side not compared: {base.reason or base.state}")
    return evaluation


def _evaluate_plan(spec: AnalysisSpec, head: SideRun, base: SideRun | None) -> Evaluation:
    new, old = _changes(head), _changes(base)
    findings: list[Finding] = []
    if base is not None:
        added = sorted(set(new) - set(old))
        removed = sorted(set(old) - set(new))
        changed = sorted(
            name
            for name in set(new) & set(old)
            if (new[name].get("digest"), new[name].get("actions"))
            != (old[name].get("digest"), old[name].get("actions"))
        )
        findings += _resource_findings(
            spec,
            added,
            removed,
            changed,
            noun="Planned resource",
            describe=lambda change, name: {
                "added": f"The plan now creates {name}.",
                "removed": f"{name} is no longer in the configuration: applying it would destroy the resource.",
                "changed": f"The planned attributes of {name} differ from the base configuration.",
            }[change],
        )
    cost = estimate_cost(list(old.values()), list(new.values()))
    if base is not None and (cost["monthly_delta"] or cost["monthly_base"] != cost["monthly_head"]):
        delta = float(cost["monthly_delta"])
        findings.append(
            Finding(
                rule="cost_estimate",
                category="cost",
                subject=f"{spec.id}::cost",
                title=f"Estimated cloud cost {'+' if delta >= 0 else '-'}${abs(delta):,.2f}/month (estimate)",
                summary=f"Estimate from {cost['basis']}; {cost['priced']} priced resource(s), unpriced types: {', '.join(cost['unpriced_types']) or 'none'}.",
                severity="medium" if delta >= 100 else "low",
                facts=(
                    ("Estimate", "yes"),
                    ("Base", f"${cost['monthly_base']:,.2f}/month"),
                    ("Head", f"${cost['monthly_head']:,.2f}/month"),
                    ("Basis", "static list prices"),
                ),
                base=Observation("estimate", f"${cost['monthly_base']:,.2f}", str(cost["monthly_base"])),
                head=Observation("estimate", f"${cost['monthly_head']:,.2f}", str(cost["monthly_head"])),
            )
        )
    heavy: dict[str, str] = {}
    head_text = head.heavy.get("plan.txt", "")
    if head_text:
        heavy["plan.txt"] = head_text
    base_text = base.heavy.get("plan.txt", "") if base is not None else ""
    if base_text and head_text and base_text != head_text:
        heavy["plan.diff"] = "\n".join(
            difflib.unified_diff(
                base_text.splitlines(), head_text.splitlines(), "base-plan", "head-plan", lineterm="", n=2
            )
        )
    actions: dict[str, int] = {}
    for row in new.values():
        for action in row.get("actions") or ():
            actions[str(action)] = actions.get(str(action), 0) + 1
    return Evaluation(
        "PASS",
        [
            f"credential-free terraform plan: {len(new)} resource change(s) against empty local state, never applied"
            + ("" if base is not None else " (no base plan to compare)")
        ],
        findings,
        summary={
            "tool": "terraform",
            "terraform_version": head.report.get("tool_version") or "",
            "resources": {"base": len(old), "head": len(new)},
            "actions": actions,
            "mock_providers": list(head.report.get("mock_providers") or ()),
            "cost": cost,
            "missing": head.report.get("missing") is True,
            "applied": False,
        },
        heavy=heavy,
    )


def _evaluate_render(spec: AnalysisSpec, head: SideRun, base: SideRun | None) -> Evaluation:
    for side, run in (("head", head), ("base", base)):
        if run is not None and run.heavy.get("rendered.invalid"):
            return Evaluation("UNKNOWN", [f"{side} side: {run.heavy['rendered.invalid']}"])
    new, new_text, problem = rendered_resources(head.heavy.get("rendered.yaml", ""))
    if problem:
        return Evaluation("UNKNOWN", [problem])
    old: dict[str, Any] = {}
    old_text = ""
    if base is not None:
        old, old_text, problem = rendered_resources(base.heavy.get("rendered.yaml", ""))
        if problem:
            return Evaluation("UNKNOWN", [f"base side: {problem}"])
    # Secret values are replaced by one constant mask before anything is compared or stored
    # (no digest of a value either): a change to only a Secret's values cannot be seen.
    secrets = sorted(
        name
        for name, document in {**old, **new}.items()
        if isinstance(document, Mapping) and str(document.get("kind") or "") == "Secret"
    )
    masked_note = " Secret values are masked, so a change to a value is not shown."
    findings: list[Finding] = []
    if base is not None:
        added = sorted(set(new) - set(old))
        removed = sorted(set(old) - set(new))
        changed = sorted(name for name in set(new) & set(old) if sha256_hex(new[name]) != sha256_hex(old[name]))
        findings = _resource_findings(
            spec,
            added,
            removed,
            changed,
            noun="Rendered resource",
            describe=lambda change, name: {
                "added": f"The rendered manifests now include {name}.",
                "removed": f"The rendered manifests no longer include {name}; deploying removes it.",
                "changed": f"The rendered {name} differs from the base render.",
            }[change]
            + (masked_note if name in secrets else ""),
        )
    reasons = [
        f"rendered {len(new)} resource(s) with {spec.tool}"
        + ("" if base is not None else " (no base render to compare)")
    ]
    if secrets:
        shown = ", ".join(secrets[:5]) + (f" and {len(secrets) - 5} more" if len(secrets) > 5 else "")
        reasons.append(
            f"Secret values are masked in the render: a change to only a Secret's values is not shown "
            f"as a delta ({shown})"
        )
    heavy: dict[str, str] = {"rendered.yaml": new_text} if new_text else {}
    if base is not None and old_text != new_text:
        heavy["rendered.diff"] = "\n".join(
            difflib.unified_diff(
                old_text.splitlines(), new_text.splitlines(), "base-render", "head-render", lineterm="", n=3
            )
        )
    return Evaluation(
        "PASS",
        reasons,
        findings,
        summary={
            "tool": spec.tool,
            "resources": {"base": len(old), "head": len(new)},
            "missing": head.report.get("missing") is True,
            "masked_secrets": len(secrets),
        },
        heavy=heavy,
    )


__all__ = [
    "DRIVER",
    "FORBIDDEN_TERRAFORM",
    "TERRAFORM_ORDER",
    "TERRAFORM_STEPS",
    "discover_specs",
    "estimate_cost",
    "evaluate",
    "parse_specs",
    "payload",
    "rendered_resources",
    "terraform_argv",
    "terraform_plan_commands",
]
