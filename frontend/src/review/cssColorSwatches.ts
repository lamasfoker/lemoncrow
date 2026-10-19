const SWATCH_ATTR = "data-lc-css-color-swatch";

const HEX_COLOR_RE = /#[0-9a-fA-F]{3,8}\b/g;
const FUNCTION_COLOR_RE = /\b(?:rgb|rgba|hsl|hsla|hwb|lab|lch|oklab|oklch|color)\([^\n;{}()]{1,220}\)/gi;

interface ColorMatch {
  start: number;
  end: number;
  value: string;
}

function validHex(value: string): boolean {
  const digits = value.length - 1;
  return digits === 3 || digits === 4 || digits === 6 || digits === 8;
}

function browserAcceptsColor(value: string): boolean {
  if (value.startsWith("#")) return validHex(value);
  if (typeof CSS !== "undefined" && typeof CSS.supports === "function") {
    try {
      if (CSS.supports("color", value)) return true;
    } catch {
      // Fall through to the style parser for older browsers/test DOMs.
    }
  }
  if (typeof document === "undefined") return true;
  const probe = document.createElement("span");
  probe.style.color = "";
  probe.style.color = value;
  // JSDOM and older engines do not understand every modern CSS color function.
  // A recognized color is definitive; an unrecognized modern function is still
  // safe to display because the candidate grammar excludes declarations/braces.
  return Boolean(probe.style.color) || /^[a-z]+\(/i.test(value);
}

export function cssColorMatches(text: string): ColorMatch[] {
  const matches: ColorMatch[] = [];
  for (const pattern of [HEX_COLOR_RE, FUNCTION_COLOR_RE]) {
    pattern.lastIndex = 0;
    for (let match = pattern.exec(text); match; match = pattern.exec(text)) {
      const value = match[0];
      if (!browserAcceptsColor(value)) continue;
      matches.push({ start: match.index, end: match.index + value.length, value });
    }
  }
  return matches
    .sort((a, b) => a.start - b.start || b.end - a.end)
    .filter((match, index, rows) => index === 0 || match.start >= rows[index - 1]!.end);
}

function textNodeAtOffset(root: HTMLElement, offset: number): { node: Text; offset: number } | null {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const parent = node.parentElement;
      return parent?.closest(`[${SWATCH_ATTR}]`) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
    },
  });
  let consumed = 0;
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node as Text;
    const length = text.data.length;
    if (offset <= consumed + length) return { node: text, offset: Math.max(0, offset - consumed) };
    consumed += length;
  }
  return null;
}

function swatch(value: string): HTMLSpanElement {
  const node = document.createElement("span");
  node.setAttribute(SWATCH_ATTR, "");
  node.setAttribute("aria-hidden", "true");
  node.title = value;
  Object.assign(node.style, {
    display: "inline-block",
    width: "10px",
    height: "10px",
    minWidth: "10px",
    marginRight: "5px",
    marginLeft: "1px",
    verticalAlign: "-1px",
    borderRadius: "2px",
    border: "1px solid color-mix(in srgb, currentColor 35%, transparent)",
    boxShadow: "inset 0 0 0 1px rgba(255,255,255,0.10)",
    backgroundColor: value,
    pointerEvents: "none",
    boxSizing: "border-box",
  });
  return node;
}

function decorateLine(line: HTMLElement): void {
  for (const existing of line.querySelectorAll<HTMLElement>(`[${SWATCH_ATTR}]`)) existing.remove();
  const text = line.textContent ?? "";
  const matches = cssColorMatches(text);
  // Insert from the end so splitting text nodes cannot invalidate offsets for
  // earlier matches on the same syntax-highlighted line.
  for (const match of [...matches].reverse()) {
    const location = textNodeAtOffset(line, match.start);
    if (!location) continue;
    const insertionPoint = location.offset === 0 ? location.node : location.node.splitText(location.offset);
    insertionPoint.parentNode?.insertBefore(swatch(match.value), insertionPoint);
  }
}

/** Add non-textual color chips before CSS color literals in Pierre-rendered lines. */
export function decorateCssColorSwatches(
  container: HTMLElement,
  path: string,
  phase: "mount" | "update" | "unmount",
): void {
  if (!path.toLowerCase().endsWith(".css")) return;
  const hosts = container.matches("diffs-container")
    ? [container]
    : [...container.querySelectorAll<HTMLElement>("diffs-container")];
  for (const host of hosts) {
    const root: ParentNode = host.shadowRoot ?? host;
    for (const existing of root.querySelectorAll<HTMLElement>(`[${SWATCH_ATTR}]`)) existing.remove();
    if (phase === "unmount") continue;
    for (const line of root.querySelectorAll<HTMLElement>("[data-line]")) decorateLine(line);
  }
}

export const CSS_COLOR_SWATCH_ATTRIBUTE = SWATCH_ATTR;
