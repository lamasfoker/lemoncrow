import { CSS_COLOR_SWATCH_ATTRIBUTE, cssColorMatches, decorateCssColorSwatches } from "./cssColorSwatches";

function renderedDiffLine(html: string): { host: HTMLElement; line: HTMLElement } {
  const container = document.createElement("div");
  const host = document.createElement("diffs-container");
  container.appendChild(host);
  const root = host.attachShadow({ mode: "open" });
  const line = document.createElement("div");
  line.setAttribute("data-line", "1");
  line.innerHTML = html;
  root.appendChild(line);
  document.body.appendChild(container);
  return { host: container, line };
}

describe("CSS color swatches", () => {
  afterEach(() => {
    document.body.innerHTML = "";
  });

  it("recognizes CSS hex and functional color literals without partial invalid hex", () => {
    expect(cssColorMatches("color:#fff; border:#11223344; background:rgb(12, 34, 56); x:#12345;").map((row) => row.value))
      .toEqual(["#fff", "#11223344", "rgb(12, 34, 56)"]);
  });

  it("inserts visual chips before literals even when syntax highlighting split the token", () => {
    const { host, line } = renderedDiffLine(
      '<span>.hero { color: </span><span>#ff</span><span>00aa</span><span>; background: rgb(12, 34, 56); }</span>',
    );
    const original = line.textContent;

    decorateCssColorSwatches(host, "styles/theme.css", "mount");

    const swatches = line.querySelectorAll<HTMLElement>(`[${CSS_COLOR_SWATCH_ATTRIBUTE}]`);
    expect(swatches).toHaveLength(2);
    expect(swatches[0]?.title).toBe("#ff00aa");
    expect(swatches[1]?.title).toBe("rgb(12, 34, 56)");
    expect(line.textContent).toBe(original);
  });

  it("is idempotent, cleans up on unmount, and ignores non-CSS files", () => {
    const { host, line } = renderedDiffLine(".hero { color: #abcdef; }");

    decorateCssColorSwatches(host, "styles/theme.css", "mount");
    decorateCssColorSwatches(host, "styles/theme.css", "update");
    expect(line.querySelectorAll(`[${CSS_COLOR_SWATCH_ATTRIBUTE}]`)).toHaveLength(1);

    decorateCssColorSwatches(host, "styles/theme.css", "unmount");
    expect(line.querySelectorAll(`[${CSS_COLOR_SWATCH_ATTRIBUTE}]`)).toHaveLength(0);

    decorateCssColorSwatches(host, "styles/theme.ts", "mount");
    expect(line.querySelectorAll(`[${CSS_COLOR_SWATCH_ATTRIBUTE}]`)).toHaveLength(0);
  });
});
