"""Built-in Storybook component previews on the versioned preview contract.

``StorybookSurfaceProvider`` turns changed ``*.stories.*`` files (and components
with a sibling story file) into ``storybook.story`` surfaces carrying a
:class:`PreviewDescriptor`. ``StorybookPreviewRenderer`` builds the static
Storybook of one exact Snapshot side inside the preview sandbox, once per
(revision, side), and captures ``iframe.html?id=<story>`` as a PNG.

The provider never executes anything, and the renderer never touches the live
checkout except to borrow lockfile-proven dependencies read-only.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

from lemoncrow.pro.capabilities.review.previews import (
    PreviewArtifact,
    PreviewRenderRequest,
    PreviewUnavailable,
)
from lemoncrow.pro.capabilities.review.surfaces import (
    PREVIEW_CONTRACT_VERSION,
    PreviewDescriptor,
    ReviewSurface,
    SetupCandidate,
    SurfaceConfigEntry,
    SurfaceContext,
    _safe_relative_path,
)

STORYBOOK_CONFIG_NAMES = ("main.js", "main.ts", "main.cjs", "main.mjs", "main.cts", "main.mts")
DEFAULT_CONFIG_DIR = ".storybook"
DEFAULT_OUTPUT_DIR = ".lemoncrow-storybook"
DEFAULT_VIEWPORT = (1200, 800)
_STORY_FILE_RE = re.compile(r"\.stories\.(?:[cm]?[jt]sx?)$")
_STORY_SUFFIXES = (".stories.tsx", ".stories.ts", ".stories.jsx", ".stories.js", ".stories.mjs")
_COMPONENT_SUFFIXES = (".tsx", ".jsx", ".ts", ".js", ".vue", ".svelte")
_MAX_STORY_FILE_BYTES = 512_000
_STORY_ID_RE = re.compile(r"^[^\s/?#&]+--[^\s/?#&]+$")
# Storybook's sanitize() character class, with its typographic punctuation spelled as escapes.
_SANITIZE_RE = re.compile("[ \u2019\u2013\u2014\u2015\u2032\u00bf'`~!@#$%^&*()_|+\\-=?;:\",.<>{}\\[\\]\\\\/]")
_EXPORT_DEFAULT_RE = re.compile(r"export\s+default\s+(\{|[A-Za-z_$][\w$]*)")
_TITLE_RE = re.compile(r"""\btitle\s*:\s*(["'`])(?P<title>(?:(?!\1).)+)\1""")
_EXPORT_DECL_RE = re.compile(r"^\s*export\s+(?:const|let|var|function\*?)\s+([A-Za-z_$][\w$]*)", re.MULTILINE)
_EXPORT_LIST_RE = re.compile(r"^\s*export\s*\{([^}]*)\}\s*;?\s*$", re.MULTILINE)
_NEXT_EXPORT_RE = re.compile(r"\n\s*export\s")
_NOT_STORIES = frozenset({"default", "__namedExportsOrder"})
# Storybook's preview iframe reports its render state as classes on <body>.
_BODY_CLASS_RE = re.compile(r"<body\b[^>]*?\bclass\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE)
_ERROR_TEXT_RE = re.compile(
    r"\bid\s*=\s*[\"']error-(message|stack)[\"'][^>]*>(.*?)</(?:h1|h2|p|pre|code|div|span)>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


def sanitize_story_part(value: str) -> str:
    """Storybook's ``sanitize`` from ``@storybook/csf``."""

    return re.sub(r"-+", "-", _SANITIZE_RE.sub("-", value.lower())).strip("-")


def story_name_from_export(key: str) -> str:
    """Storybook's ``toStartCaseStr``: ``PrimaryButton`` -> ``Primary Button``."""

    value = key.replace("_", " ").replace("-", " ").replace(".", " ")
    value = re.sub(r"([^\n])([A-Z])([a-z])", r"\1 \2\3", value)
    value = re.sub(r"([a-z])([A-Z])", r"\1 \2", value)
    value = re.sub(r"([a-z])([0-9])", r"\1 \2", value, flags=re.IGNORECASE)
    value = re.sub(r"([0-9])([a-z])", r"\1 \2", value, flags=re.IGNORECASE)
    value = re.sub(r"(\s|^)(\w)", lambda match: match.group(1) + match.group(2).upper(), value)
    return re.sub(r" +", " ", value).strip()


def story_id(title: str, export_name: str) -> str:
    return f"{sanitize_story_part(title)}--{sanitize_story_part(story_name_from_export(export_name))}"


def _meta_region(text: str) -> str:
    match = _EXPORT_DEFAULT_RE.search(text)
    if match is None:
        return text
    if match.group(1) == "{":
        region = text[match.end() - 1 :]
    else:
        declaration = re.search(rf"(?:const|let|var)\s+{re.escape(match.group(1))}\b[^=]*=", text)
        region = text[declaration.end() :] if declaration is not None else text
    following = _NEXT_EXPORT_RE.search(region)
    return region[: following.start()] if following is not None else region


def auto_title(story_path: str, root: str, stories_dir: str = "src") -> str:
    """Approximate Storybook's autotitle for a story file without an explicit title."""

    relative = PurePosixPath(story_path)
    if root not in {"", "."}:
        relative = relative.relative_to(root)
    parts = list(relative.parts)
    stories_parts = [part for part in PurePosixPath(stories_dir).parts if part not in {"", "."}]
    if stories_parts and parts[: len(stories_parts)] == stories_parts:
        parts = parts[len(stories_parts) :]
    if parts:
        parts[-1] = _STORY_FILE_RE.sub("", parts[-1])
    if len(parts) >= 2 and (parts[-1] == parts[-2] or parts[-1] == "index"):
        parts.pop()
    return "/".join(parts)


def parse_story_file(text: str) -> tuple[str, tuple[str, ...]]:
    """Return (explicit title or "", story export names) for one CSF file.

    lc-debt: regex CSF parsing ignores includeStories/excludeStories and computed
    titles; an AST parse (e.g. via the project's own csf-tools) is the upgrade path.
    """

    title_match = _TITLE_RE.search(_meta_region(text))
    title = title_match.group("title").strip() if title_match is not None else ""
    names: list[str] = [match.group(1) for match in _EXPORT_DECL_RE.finditer(text)]
    for block in _EXPORT_LIST_RE.finditer(text):
        for raw in block.group(1).split(","):
            item = raw.strip()
            if not item:
                continue
            alias = item.split(" as ", 1)[-1].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", alias):
                names.append(alias)
    return title, tuple(name for name in dict.fromkeys(names) if name not in _NOT_STORIES)


def _has_config(directory: Path, config_dir: str) -> bool:
    return any((directory / config_dir / name).is_file() for name in STORYBOOK_CONFIG_NAMES)


def _viewport(value: object) -> tuple[int, int]:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return max(320, min(int(value[0]), 2560)), max(240, min(int(value[1]), 4000))
        except (TypeError, ValueError):
            pass
    return DEFAULT_VIEWPORT


class StorybookSurfaceProvider:
    provider_id = "storybook"

    def _roots(self, context: SurfaceContext) -> dict[str, SurfaceConfigEntry]:
        roots: dict[str, SurfaceConfigEntry] = {}
        for entry in context.config.for_provider(self.provider_id):
            roots.setdefault(entry.string("root", ".") or ".", entry)
        return roots

    @staticmethod
    def _root_for(path: str, repo_root: Path, configured: dict[str, SurfaceConfigEntry]) -> str | None:
        parents = [
            PurePosixPath(*PurePosixPath(path).parts[:depth])
            for depth in range(len(PurePosixPath(path).parts) - 1, -1, -1)
        ]
        for parent in parents:
            candidate = parent.as_posix() if parent.parts else "."
            if candidate in configured:
                return candidate
        for parent in parents:
            candidate = parent.as_posix() if parent.parts else "."
            if _has_config(repo_root / candidate, DEFAULT_CONFIG_DIR):
                return candidate
        return None

    @staticmethod
    def _story_files(repo_root: Path, path: str) -> tuple[str, ...]:
        if _STORY_FILE_RE.search(path):
            return (path,)
        pure = PurePosixPath(path)
        if pure.suffix not in _COMPONENT_SUFFIXES:
            return ()
        stem = pure.name[: -len(pure.suffix)]
        rows = []
        for suffix in _STORY_SUFFIXES:
            sibling = (pure.parent / f"{stem}{suffix}").as_posix()
            if (repo_root / sibling).is_file():
                rows.append(sibling)
        return tuple(rows)

    def discover(self, context: SurfaceContext) -> tuple[ReviewSurface, ...]:
        configured = self._roots(context)
        surfaces: list[ReviewSurface] = []
        for path in context.changed_paths:
            try:
                _safe_relative_path(path, field_name="changed path")
            except ValueError:
                continue
            root = self._root_for(path, context.repo_root, configured)
            if root is None:
                continue
            entry = configured.get(root)
            config_dir = (entry.string("config_dir", DEFAULT_CONFIG_DIR) if entry else "") or DEFAULT_CONFIG_DIR
            if entry is None and not _has_config(context.repo_root / root, config_dir):
                continue
            stories_dir = (entry.string("stories_dir", "src") if entry else "src") or "src"
            viewport = _viewport(entry.values.get("viewport") if entry else None)
            for story_file in self._story_files(context.repo_root, path):
                source = context.repo_root / story_file
                try:
                    if source.stat().st_size > _MAX_STORY_FILE_BYTES:
                        continue
                    text = source.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                title, exports = parse_story_file(text)
                title = title or auto_title(story_file, root, stories_dir)
                if not title:
                    continue
                for export_name in exports:
                    identifier = story_id(title, export_name)
                    surfaces.append(
                        ReviewSurface(
                            id=identifier if root == "." else f"{root}:{identifier}",
                            provider=self.provider_id,
                            kind="storybook.story",
                            title=f"{title} · {story_name_from_export(export_name)}",
                            locator=identifier,
                            affected_paths=tuple(dict.fromkeys((path, story_file))),
                            capabilities=("preview", "compare", "source"),
                            metadata={
                                "root": root,
                                "story_file": story_file,
                                "story_id": identifier,
                                "component_title": title,
                                "export": export_name,
                                "surface_id": entry.id if entry is not None else "",
                            },
                            preview=PreviewDescriptor(
                                renderer=StorybookPreviewRenderer.renderer_id,
                                display="image",
                                media_type="image/png",
                                label="Storybook story",
                                scope=root,
                                options={
                                    "root": root,
                                    "config_dir": config_dir,
                                    "story_id": identifier,
                                    "story_file": story_file,
                                    "viewport": list(viewport),
                                },
                            ),
                        )
                    )
        return tuple(surfaces)

    def setup_candidates(self, repo_root: Path) -> tuple[SetupCandidate, ...]:
        from lemoncrow.pro.capabilities.review.surface_providers import _relative, _walk_named

        candidates: list[SetupCandidate] = []
        for config in _walk_named(repo_root, set(STORYBOOK_CONFIG_NAMES)):
            if config.parent.name != DEFAULT_CONFIG_DIR:
                continue
            root = _relative(repo_root, config.parent.parent)
            if any(candidate.root == root for candidate in candidates):
                continue
            depth = 0 if root == "." else len(PurePosixPath(root).parts)
            candidates.append(
                SetupCandidate(
                    id="storybook" if root == "." else "storybook-" + root.replace("/", "-"),
                    provider=self.provider_id,
                    kind="storybook.story",
                    root=root,
                    confidence=1.0,
                    auto_write=depth <= 2,
                    details={"config_dir": DEFAULT_CONFIG_DIR},
                )
            )
        return tuple(candidates)


def storybook_render_state(dom: str) -> tuple[str, str]:
    """Classify a captured Storybook iframe: rendered, error, no-preview, loading, or unknown.

    Returns the state and, for an error, the message Storybook displayed.
    """

    match = _BODY_CLASS_RE.search(dom)
    classes = set(match.group(1).split()) if match else set()
    if "sb-show-errordisplay" in classes:
        texts: dict[str, str] = {}
        for kind, raw in _ERROR_TEXT_RE.findall(dom):
            text = " ".join(html.unescape(_TAG_RE.sub(" ", raw)).split())
            if text:
                texts.setdefault(kind.lower(), text)
        stack_head = texts.get("stack", "").split(" at ", 1)[0]
        return "error", (texts.get("message") or stack_head)[:300]
    if "sb-show-nopreview" in classes:
        return "no-preview", ""
    if classes & {"sb-show-preparing-story", "sb-show-preparing-docs"}:
        return "loading", ""
    if "sb-show-main" in classes:
        return "rendered", ""
    return "unknown", ""


class StorybookPreviewRenderer:
    renderer_id: ClassVar[str] = "storybook"
    contract_version: ClassVar[int] = PREVIEW_CONTRACT_VERSION

    @staticmethod
    def _index_ids(static: Path) -> set[str] | None:
        for name, key in (("index.json", "entries"), ("stories.json", "stories")):
            try:
                payload = json.loads((static / name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            entries = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(entries, dict):
                return set(entries)
        return None

    @staticmethod
    def _build_command(request: PreviewRenderRequest, node_modules: Path, config_dir: str, output: str) -> list[str]:
        configured: Any = request.config.values.get("build_command") if request.config is not None else None
        if isinstance(configured, list) and configured and all(isinstance(item, str) and item for item in configured):
            return list(configured)
        if (node_modules / ".bin" / "storybook").exists():
            return [
                "node_modules/.bin/storybook",
                "build",
                "--config-dir",
                config_dir,
                "--output-dir",
                output,
                "--quiet",
                "--disable-telemetry",
            ]
        if (node_modules / ".bin" / "build-storybook").exists():
            return ["node_modules/.bin/build-storybook", "--config-dir", config_dir, "--output-dir", output, "--quiet"]
        raise PreviewUnavailable("the Storybook CLI is not installed in the checkout's node_modules")

    def render(self, request: PreviewRenderRequest) -> PreviewArtifact:
        options = request.descriptor.options
        root = str(options.get("root") or ".")
        story = str(options.get("story_id") or "")
        story_file = str(options.get("story_file") or "")
        config_dir = str(options.get("config_dir") or DEFAULT_CONFIG_DIR)
        if not _STORY_ID_RE.match(story):
            return PreviewArtifact.failed(f"invalid Storybook story id: {story!r}")
        output_name = DEFAULT_OUTPUT_DIR
        if request.config is not None and request.config.string("output_dir"):
            output_name = _safe_relative_path(request.config.string("output_dir"), field_name="storybook output_dir")
        project = request.path(root)
        if story_file and not request.path(story_file).is_file():
            return PreviewArtifact.unavailable(f"{story_file} does not exist on the {request.side} side")
        if not _has_config(project, config_dir):
            return PreviewArtifact.unavailable(f"Storybook config {config_dir} is missing on the {request.side} side")
        static = request.path(output_name if root == "." else f"{root}/{output_name}")
        if not (static / "iframe.html").is_file():
            node_modules = request.sandbox.node_modules(root)
            command = self._build_command(request, node_modules, config_dir, output_name)
            result = request.sandbox.run(
                command,
                cwd=root,
                node_modules=node_modules,
                env={"STORYBOOK_DISABLE_TELEMETRY": "1", "NODE_ENV": "production"},
            )
            if result.exit_code != 0:
                limit = f"; {result.limit_note}" if result.limit_note else ""
                return PreviewArtifact.failed(
                    f"Storybook build failed on the {request.side} side (exit {result.exit_code}{limit})",
                    output=result.output,
                )
            if not (static / "iframe.html").is_file():
                return PreviewArtifact.failed("Storybook build produced no iframe.html", output=result.output)
        known = self._index_ids(static)
        if known is not None and story not in known:
            return PreviewArtifact.unavailable(f"story {story} does not exist in the {request.side} side's Storybook")
        width, height = _viewport(options.get("viewport"))
        capture = request.sandbox.capture(
            static,
            f"/iframe.html?id={story}&viewMode=story",
            width=width,
            height=height,
            filename=f"{request.side}.png",
        )
        # A story that throws still produces a screenshot (of Storybook's error
        # screen); report it as the failure it is, with Storybook's message.
        state, detail = storybook_render_state(capture.dom)
        if state == "error":
            message = f": {detail}" if detail else ""
            return PreviewArtifact.failed(f"story {story} threw while rendering on the {request.side} side{message}")
        if state == "no-preview":
            return PreviewArtifact.unavailable(
                f"Storybook showed no preview for story {story} on the {request.side} side"
            )
        if state == "loading":
            return PreviewArtifact.failed(
                f"story {story} was still loading on the {request.side} side when the capture finished"
            )
        return PreviewArtifact.ready(
            capture.path,
            "image/png",
            filename=f"{story}-{request.side}.png",
            summary=f"Storybook story {story} · {width}x{height}",
        )


__all__ = [
    "StorybookPreviewRenderer",
    "StorybookSurfaceProvider",
    "auto_title",
    "parse_story_file",
    "sanitize_story_part",
    "story_id",
    "story_name_from_export",
    "storybook_render_state",
]
