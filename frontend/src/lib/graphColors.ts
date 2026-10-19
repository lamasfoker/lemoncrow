import type { CodeMapActivityKind } from "../api";

// Single vivid color per symbol kind (used when the server sends no color).
export const KIND_COLORS: Record<string, string> = {
  class: "#f59e0b",
  method: "#06b6d4",
  function: "#06b6d4",
  async_function: "#6366f1",
  interface: "#8b5cf6",
  type: "#8b5cf6",
  module: "#22c55e",
  file: "#64748b",
  reference: "#94a3b8",
};

// Stable raw palette used for repository/module communities. vivid adapts
// these to the active light/dark theme, so the same community keeps its hue.
const COMMUNITY_PALETTE = [
  "#14b8a6",
  "#3b82f6",
  "#8b5cf6",
  "#f59e0b",
  "#ec4899",
  "#22c55e",
  "#06b6d4",
  "#f97316",
  "#6366f1",
  "#84cc16",
  "#a855f7",
  "#0ea5e9",
] as const;

// Raw semantic relationship colors. They are intentionally mid-tone; vivid
// makes them readable on both themes without changing relationship identity.
const RELATION_COLORS: Record<string, string> = {
  calls: "#64748b",
  imports: "#0ea5e9",
  references: "#8b5cf6",
  impacts: "#f59e0b",
  contains: "#94a3b8",
  dependency: "#14b8a6",
};

// [dark-theme color, light-theme color].
export const ACTIVITY_COLORS: Record<CodeMapActivityKind, [string, string]> = {
  search: ["#c4b5fd", "#6d28d9"],
  read: ["#67e8f9", "#0e7490"],
  edit: ["#fbbf24", "#b45309"],
  verify: ["#4ade80", "#15803d"],
};

export function stableUnit(value: string): number {
  let hash = 2166136261;
  for (let i = 0; i < value.length; i += 1) {
    hash ^= value.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0) / 4294967295;
}

export function communityColor(value: string): string {
  const index = Math.min(
    COMMUNITY_PALETTE.length - 1,
    Math.floor(stableUnit(value || "root") * COMMUNITY_PALETTE.length)
  );
  return COMMUNITY_PALETTE[index];
}

export function relationColor(kind: string): string {
  return RELATION_COLORS[kind] ?? "#64748b";
}

// Punch up server/palette colors: saturate and pin lightness so graph marks
// stay readable instead of turning into washed-out pastels. Near-greys remain
// neutral so structural/context marks do not become random hues.
export function vivid(hex: string, light: boolean): string {
  const match = /^#?([\da-f]{6})$/i.exec(hex.trim());
  if (!match) return hex;
  const int = parseInt(match[1], 16);
  const r = ((int >> 16) & 255) / 255;
  const g = ((int >> 8) & 255) / 255;
  const b = (int & 255) / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const delta = max - min;
  const lightness = (max + min) / 2;
  if (delta < 0.05) return hex;
  let hue = 0;
  if (max === r) hue = ((g - b) / delta) % 6;
  else if (max === g) hue = (b - r) / delta + 2;
  else hue = (r - g) / delta + 4;
  hue = (hue * 60 + 360) % 360;
  const sat = 0.88;
  const lum = light
    ? Math.min(0.47, Math.max(0.36, lightness))
    : Math.min(0.68, Math.max(0.56, lightness));
  const c = (1 - Math.abs(2 * lum - 1)) * sat;
  const x = c * (1 - Math.abs(((hue / 60) % 2) - 1));
  const m = lum - c / 2;
  let rr = 0;
  let gg = 0;
  let bb = 0;
  if (hue < 60) [rr, gg, bb] = [c, x, 0];
  else if (hue < 120) [rr, gg, bb] = [x, c, 0];
  else if (hue < 180) [rr, gg, bb] = [0, c, x];
  else if (hue < 240) [rr, gg, bb] = [0, x, c];
  else if (hue < 300) [rr, gg, bb] = [x, 0, c];
  else [rr, gg, bb] = [c, 0, x];
  const channel = (value: number) =>
    Math.round((value + m) * 255)
      .toString(16)
      .padStart(2, "0");
  return "#" + channel(rr) + channel(gg) + channel(bb);
}
