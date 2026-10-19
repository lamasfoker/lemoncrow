// Pure view helpers for `/code`: session marks, file-tree marks, map
// locations and the live overlay. Kept free of React so they are unit tested.

import type {
  CodeChanges,
  CodeChangedFile,
  CodeFocusLevel,
  CodeMapActivityEvent,
  CodeMapActivityKind,
  CodeMapActivitySession,
  CodeMapNode,
  CodeModule,
} from "../api";
import type { MapNode, MapPayload } from "../map/types";

/** How long an activity event keeps a node lit in the live overlay. */
export const LIVE_WINDOW_MS = 90_000;

/**
 * One short, stable mark per session (A, B, C…), shared by the Explorer,
 * the map overlay and the Changes menu. Sessions that authored changes come
 * first, in the order the changes endpoint lists them, then live ones.
 */
export function sessionMarks(
  changes: Pick<CodeChanges, "sessions"> | null,
  activity: readonly Pick<CodeMapActivitySession, "id">[],
): Map<string, string> {
  const marks = new Map<string, string>();
  const ids = [...(changes?.sessions ?? []).map((session) => session.id), ...activity.map((session) => session.id)];
  for (const id of ids) {
    if (marks.has(id)) continue;
    const index = marks.size;
    marks.set(id, index < 26 ? String.fromCharCode(65 + index) : String(index + 1));
  }
  return marks;
}

export function sessionLabel(session: { host: string; session_id: string; title?: string }): string {
  const title = (session.title ?? "").trim();
  return title || `${session.host || "agent"} ${session.session_id.slice(0, 8)}`;
}

/** How a hotspot window reads in a sentence: "this week", or the day count. */
export function windowPhrase(days: number): string {
  return days === 7 ? "this week" : `in the last ${days} days`;
}

/** Folder path of a file; "" at the repository root. */
export function folderOf(path: string): string {
  const slash = path.lastIndexOf("/");
  return slash > 0 ? path.slice(0, slash) : "";
}

export interface FolderRollup {
  changed: number;
  live: boolean;
}

/** Uncommitted and live counts rolled up to every ancestor folder. */
export function folderRollups(
  changed: ReadonlyMap<string, CodeChangedFile>,
  live: ReadonlyMap<string, CodeMapActivityKind>,
): Map<string, FolderRollup> {
  const out = new Map<string, FolderRollup>();
  const visit = (path: string, apply: (row: FolderRollup) => void) => {
    let folder = folderOf(path);
    while (folder) {
      const row = out.get(folder) ?? { changed: 0, live: false };
      apply(row);
      out.set(folder, row);
      folder = folderOf(folder);
    }
  };
  for (const path of changed.keys()) visit(path, (row) => { row.changed += 1; });
  for (const path of live.keys()) visit(path, (row) => { row.live = true; });
  return out;
}

/** The newest activity kind per path within the live window. */
export function liveByPath(events: readonly CodeMapActivityEvent[], now: number): Map<string, CodeMapActivityKind> {
  const out = new Map<string, CodeMapActivityKind>();
  for (const event of events) {
    if (!event.path) continue;
    const at = Date.parse(event.at);
    if (Number.isFinite(at) && now - at > LIVE_WINDOW_MS) continue;
    out.set(event.path, event.kind);
  }
  return out;
}

export type MapSpec = { level: CodeFocusLevel; id: string } | null;

/** `map=` URL value: `module:<id>`, `file:<path>`, `symbol:<id>`, or the overview. */
export function parseMapSpec(value: string | null): MapSpec {
  if (!value) return null;
  const colon = value.indexOf(":");
  if (colon <= 0) return null;
  const level = value.slice(0, colon);
  const id = value.slice(colon + 1);
  if (level === "module" || level === "file" || level === "symbol") return { level, id };
  return null;
}

export function formatMapSpec(spec: MapSpec): string | null {
  return spec ? `${spec.level}:${spec.id}` : null;
}

/** The module a file belongs to: the longest module path that contains it. */
export function moduleOfPath(path: string, modules: readonly Pick<CodeModule, "id" | "kind">[]): string | null {
  let best: string | null = null;
  for (const module of modules) {
    if (module.kind === "file") {
      if (module.id === path) return module.id;
      continue;
    }
    if (module.id === "" ? !path.includes("/") || best === null : path.startsWith(module.id + "/")) {
      if (best === null || module.id.length > best.length) best = module.id;
    }
  }
  return best;
}

export interface LiveMarks {
  kind: CodeMapActivityKind;
  sessions: string[];
}

/** Live overlay per path: what is happening and which session marks. */
export function liveMarksByPath(
  byPath: ReadonlyMap<string, CodeMapActivityKind>,
  sessionOf: ReadonlyMap<string, string>,
  marks: ReadonlyMap<string, string>,
  followed: string | null,
): Map<string, LiveMarks> {
  const out = new Map<string, LiveMarks>();
  const mark = followed ? marks.get(followed) : undefined;
  for (const [path, kind] of byPath) {
    const session = sessionOf.get(path) ?? followed;
    const label = session ? marks.get(session) : mark;
    out.set(path, { kind, sessions: label ? [label] : [] });
  }
  return out;
}

/** Focus payload with uncommitted and live marks applied to its nodes. */
export function overlayFocus<P extends MapPayload>(payload: P, live: ReadonlyMap<string, LiveMarks>): P {
  if (!live.size) return payload;
  const nodes = payload.graph.nodes.map((node: MapNode) => {
    const marks = live.get(node.path);
    return marks ? { ...node, activity: marks.kind, sessions: marks.sessions } : node;
  });
  return { ...payload, graph: { ...payload.graph, nodes } };
}

/** Files a session filter keeps in the Explorer, or null for no filter. */
export function sessionFileFilter(changes: CodeChanges | null, session: string | null): Set<string> | null {
  if (!session || !changes) return null;
  if (session === "unattributed") return new Set(changes.unattributed);
  const match = changes.sessions.find((row) => row.id === session);
  return match ? new Set(match.files) : null;
}

/** The narrowest symbol that contains a line, else the file node. */
export function nodeAtLine(nodes: readonly CodeMapNode[], path: string, line: number): CodeMapNode | undefined {
  const inFile = nodes.filter((node) => node.path === path);
  const symbol = inFile
    .filter((node) => node.node_type !== "file" && node.line <= line && node.end_line >= line)
    .sort((a, b) => a.end_line - a.line - (b.end_line - b.line))[0];
  return symbol ?? inFile.find((node) => node.node_type === "file") ?? inFile[0];
}
