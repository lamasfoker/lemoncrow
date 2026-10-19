"""Standards-based supply-chain evidence helpers for Review.

Review stores the original SPDX/CycloneDX/provenance/scan bytes as immutable
Snapshot evidence.  This module validates and projects inspectable metadata; it
does not invent a LemonCrow SBOM format or a supply-chain score.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class SbomComponent:
    name: str
    version: str = ""
    license: str = ""
    purl: str = ""


@dataclass(frozen=True)
class SbomInspection:
    standard: str
    version: str
    component_count: int
    components: tuple[SbomComponent, ...] = ()


@dataclass(frozen=True)
class VulnerabilityFinding:
    package: str
    advisory: str = ""
    severity: str = ""


def validate_artifact_digest(value: str) -> str:
    digest = value.strip().lower()
    if not _SHA256.fullmatch(digest):
        raise ValueError("artifact_digest must be an immutable sha256:<64 hex> digest")
    return digest


def _license_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        expression = value.get("expression")
        if expression:
            return str(expression)
        license_value = value.get("license")
        if isinstance(license_value, Mapping):
            return str(license_value.get("id") or license_value.get("name") or "")
        return str(value.get("id") or value.get("name") or "")
    return ""


def inspect_sbom(payload: bytes) -> SbomInspection:
    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("SBOM must be JSON SPDX or CycloneDX") from exc
    if not isinstance(raw, dict):
        raise ValueError("SBOM root must be a JSON object")

    spdx_version = str(raw.get("spdxVersion") or "")
    if spdx_version in {"SPDX-2.2", "SPDX-2.3"}:
        packages = raw.get("packages")
        if not isinstance(packages, list):
            raise ValueError("SPDX SBOM must contain a packages array")
        components: list[SbomComponent] = []
        for package in packages:
            if not isinstance(package, Mapping):
                continue
            purl = ""
            refs = package.get("externalRefs")
            if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes)):
                for ref in refs:
                    if not isinstance(ref, Mapping):
                        continue
                    locator = str(ref.get("referenceLocator") or "")
                    if locator.startswith("pkg:"):
                        purl = locator
                        break
            license_value = str(package.get("licenseConcluded") or package.get("licenseDeclared") or "")
            if license_value in {"NOASSERTION", "NONE"}:
                license_value = ""
            components.append(
                SbomComponent(
                    name=str(package.get("name") or ""),
                    version=str(package.get("versionInfo") or ""),
                    license=license_value,
                    purl=purl,
                )
            )
        return SbomInspection("SPDX", spdx_version, len(packages), tuple(components))

    if str(raw.get("bomFormat") or "") == "CycloneDX":
        version = str(raw.get("specVersion") or "")
        if not version:
            raise ValueError("CycloneDX SBOM must identify specVersion")
        components_raw = raw.get("components")
        if not isinstance(components_raw, list):
            raise ValueError("CycloneDX SBOM must contain a components array")
        cyclonedx_components: list[SbomComponent] = []
        for component in components_raw:
            if not isinstance(component, Mapping):
                continue
            licenses = component.get("licenses")
            license_value = ""
            if isinstance(licenses, list):
                license_value = ", ".join(filter(None, (_license_text(item) for item in licenses)))
            cyclonedx_components.append(
                SbomComponent(
                    name=str(component.get("name") or ""),
                    version=str(component.get("version") or ""),
                    license=license_value,
                    purl=str(component.get("purl") or ""),
                )
            )
        return SbomInspection("CycloneDX", version, len(components_raw), tuple(cyclonedx_components))

    raise ValueError("unsupported SBOM format; expected SPDX 2.2/2.3 or CycloneDX JSON")


def inspect_json_evidence(payload: bytes, *, kind: str) -> dict[str, Any]:
    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{kind} evidence must be JSON") from exc
    if not isinstance(raw, (dict, list)):
        raise ValueError(f"{kind} evidence must be a JSON object or array")
    if kind == "provenance" and isinstance(raw, dict):
        predicate = raw.get("predicate")
        if not (
            raw.get("buildType")
            or raw.get("predicateType")
            or raw.get("_type")
            or (isinstance(predicate, Mapping) and predicate.get("buildDefinition"))
        ):
            raise ValueError("provenance JSON has no recognizable SLSA/in-toto identity")
    return {"records": len(raw) if isinstance(raw, list) else 1}


def _package_name(value: object) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, Mapping):
        return ""
    return str(value.get("packageName") or value.get("name") or value.get("package") or "")


def vulnerability_findings(payload: bytes) -> tuple[VulnerabilityFinding, ...]:
    """Extract common scanner package/advisory facts without scoring policy.

    Supports Artifact Analysis-like rows and common OSV/SARIF-ish package
    shapes. Unknown schemas simply produce no component association while the
    original scan remains downloadable evidence.
    """

    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ()
    rows: list[object]
    if isinstance(raw, list):
        rows = list(raw)
    elif isinstance(raw, Mapping):
        candidate = raw.get("results") or raw.get("findings") or raw.get("vulnerabilities")
        rows = list(candidate) if isinstance(candidate, list) else [raw]
    else:
        return ()

    findings: list[VulnerabilityFinding] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        vulnerability = row.get("vulnerability")
        vuln = vulnerability if isinstance(vulnerability, Mapping) else row
        advisory = str(
            row.get("noteName") or row.get("advisory") or row.get("id") or vuln.get("id") or vuln.get("name") or ""
        )
        severity = str(vuln.get("effectiveSeverity") or vuln.get("severity") or row.get("severity") or "")
        names: set[str] = set()
        for key in ("package", "affectedPackage", "packageName"):
            value = vuln.get(key) if isinstance(vuln, Mapping) else None
            name = _package_name(value)
            if name:
                names.add(name)
        issues = vuln.get("packageIssue") if isinstance(vuln, Mapping) else None
        if isinstance(issues, list):
            for issue in issues:
                if not isinstance(issue, Mapping):
                    continue
                for key in ("affectedPackage", "fixedPackage", "package"):
                    name = _package_name(issue.get(key))
                    if name:
                        names.add(name)
        affected = vuln.get("affected") if isinstance(vuln, Mapping) else None
        if isinstance(affected, list):
            for item in affected:
                if not isinstance(item, Mapping):
                    continue
                name = _package_name(item.get("package"))
                if name:
                    names.add(name)
        for name in names:
            finding_key = (name.lower(), advisory, severity)
            if finding_key in seen:
                continue
            seen.add(finding_key)
            findings.append(VulnerabilityFinding(package=name, advisory=advisory, severity=severity))
    return tuple(findings)


def parse_evidence_metadata(detail: str) -> dict[str, Any]:
    try:
        raw = json.loads(detail)
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


__all__ = [
    "SbomComponent",
    "SbomInspection",
    "VulnerabilityFinding",
    "inspect_json_evidence",
    "inspect_sbom",
    "parse_evidence_metadata",
    "validate_artifact_digest",
    "vulnerability_findings",
]
