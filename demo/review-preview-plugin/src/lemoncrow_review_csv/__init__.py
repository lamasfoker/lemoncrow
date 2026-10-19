"""Example third-party LemonCrow Review preview plugin.

One surface provider plus one preview renderer, registered through the
``lemoncrow.review_surface_providers`` and ``lemoncrow.review_preview_renderers``
entry point groups. Changed ``*.csv`` files become ``data.table`` surfaces whose
old and new sides are rendered as static HTML tables.

The renderer only reads data from the host-materialized Snapshot workspace, so
it needs no sandbox. A renderer that must execute reviewed code (a build, a
screenshot, a CLI) must do it through ``request.sandbox`` instead.
"""

from __future__ import annotations

import csv
import fnmatch
import html
import io
from pathlib import Path, PurePosixPath

from lemoncrow.pro.capabilities.review.previews import PreviewArtifact, PreviewRenderRequest
from lemoncrow.pro.capabilities.review.surfaces import (
    PreviewDescriptor,
    ReviewSurface,
    SetupCandidate,
    SurfaceContext,
)

PROVIDER_ID = "csv-table"
RENDERER_ID = "csv-table"
MAX_ROWS = 500
MAX_COLUMNS = 50


class CsvTableSurfaceProvider:
    """Discover changed CSV files matched by configured ``paths`` globs."""

    provider_id = PROVIDER_ID

    def discover(self, context: SurfaceContext) -> tuple[ReviewSurface, ...]:
        surfaces: list[ReviewSurface] = []
        for entry in context.config.for_provider(self.provider_id):
            patterns = entry.strings("paths") or ("**/*.csv", "*.csv")
            for path in context.changed_paths:
                if not path.endswith(".csv") or not any(fnmatch.fnmatch(path, pattern) for pattern in patterns):
                    continue
                parent = PurePosixPath(path).parent.as_posix()
                surfaces.append(
                    ReviewSurface(
                        id=path,
                        provider=self.provider_id,
                        kind="data.table",
                        title=PurePosixPath(path).name,
                        locator=path,
                        affected_paths=(path,),
                        capabilities=("preview", "compare", "source"),
                        metadata={"surface_id": entry.id, "path": path},
                        preview=PreviewDescriptor(
                            renderer=RENDERER_ID,
                            display="html",
                            media_type="text/html",
                            label="CSV table",
                            # Materialize only the file's directory, not the whole repository.
                            scope=parent,
                            options={"path": path},
                        ),
                    )
                )
        return tuple(surfaces)

    def setup_candidates(self, _repo_root: Path) -> tuple[SetupCandidate, ...]:
        # Opt-in only: CSV files are too common to auto-configure.
        return ()


def _table(rows: list[list[str]], *, truncated_rows: int, truncated_columns: bool) -> str:
    head, *body = rows or [[]]
    cells = "".join(f"<th>{html.escape(cell)}</th>" for cell in head)
    lines = "".join("<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in row) + "</tr>" for row in body)
    notes = []
    if truncated_rows:
        notes.append(f"{truncated_rows} more row(s) not shown")
    if truncated_columns:
        notes.append(f"only the first {MAX_COLUMNS} columns are shown")
    note = f"<p class=note>{html.escape('; '.join(notes))}</p>" if notes else ""
    return (
        "<!doctype html><html><head><meta charset=utf-8><style>"
        "body{font:12px ui-monospace,monospace;margin:12px;color:#1f2328;background:#fff}"
        "table{border-collapse:collapse}th,td{border:1px solid #d0d7de;padding:3px 8px;text-align:left}"
        "th{background:#f6f8fa}.note{color:#57606a}</style></head><body>"
        f"<table><thead><tr>{cells}</tr></thead><tbody>{lines}</tbody></table>{note}</body></html>"
    )


class CsvTableRenderer:
    """Render one Snapshot side of a CSV file as a bounded HTML table."""

    renderer_id = RENDERER_ID
    contract_version = 1

    def render(self, request: PreviewRenderRequest) -> PreviewArtifact:
        path = str(request.descriptor.options.get("path") or "")
        source = request.path(path)
        if not source.is_file():
            change = "added" if request.side == "old" else "deleted"
            return PreviewArtifact.unavailable(
                f"{path} does not exist on the {request.side} side ({change} by this change)"
            )
        text = source.read_text(encoding="utf-8", errors="replace")
        rows: list[list[str]] = []
        total = 0
        wide = False
        for row in csv.reader(io.StringIO(text)):
            total += 1
            if len(rows) <= MAX_ROWS:
                wide = wide or len(row) > MAX_COLUMNS
                rows.append(row[:MAX_COLUMNS])
        target = request.output_dir / "table.html"
        target.write_text(
            _table(rows, truncated_rows=max(0, total - len(rows)), truncated_columns=wide),
            encoding="utf-8",
        )
        return PreviewArtifact.ready(
            target,
            "text/html",
            filename=f"{PurePosixPath(path).stem}-{request.side}.html",
            summary=f"{max(0, total - 1)} data row(s)",
        )


__all__ = ["CsvTableRenderer", "CsvTableSurfaceProvider"]
