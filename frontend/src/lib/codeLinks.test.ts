import { describe, expect, it } from "vitest";

import { codeHref, currentReturnPath, returnPathFrom, safeReturnPath, withReturnPath } from "./codeLinks";

describe("codeLinks", () => {
  it("follows only in-app paths back", () => {
    expect(safeReturnPath("/r/abc?map=file")).toBe("/r/abc?map=file");
    for (const unsafe of ["//evil.example/x", "/\\evil.example", "https://evil.example", "javascript:alert(1)", "code", "", null, undefined]) {
      expect(safeReturnPath(unsafe)).toBe("");
    }
    expect(returnPathFrom(new URLSearchParams("from=%2F%2Fevil.example"))).toBe("");
    expect(returnPathFrom(new URLSearchParams("from=%2Fcode%3Ffile%3Da.ts"))).toBe("/code?file=a.ts");
  });

  it("keeps the trail one step long, so links never nest", () => {
    expect(currentReturnPath({ pathname: "/r/abc", search: "?map=file&from=%2Fcode%3Ffile%3Da.ts" })).toBe("/r/abc?map=file");
    expect(currentReturnPath({ pathname: "/code", search: "" })).toBe("/code");
    const hop = withReturnPath("/r/abc?from=%2Fold", "/code?file=a.ts");
    expect(new URL(hop, "http://x").searchParams.getAll("from")).toEqual(["/code?file=a.ts"]);
    expect(withReturnPath("/r/abc", "//evil.example")).toBe("/r/abc");
  });

  it("opens a file or a line range in Code with its way back", () => {
    const file = new URL(codeHref("/repo", { path: "src/a.ts", line: 0, endLine: 0 }, "/r/abc?map=architecture"), "http://x");
    expect(file.pathname).toBe("/code");
    expect(Object.fromEntries(file.searchParams)).toEqual({ repo: "/repo", file: "src/a.ts", view: "source", from: "/r/abc?map=architecture" });

    const range = new URL(codeHref("/repo", { path: "src/a.ts", line: 12, endLine: 20 }, "/r/abc"), "http://x");
    expect(range.searchParams.get("line")).toBe("12");
    expect(range.searchParams.get("end")).toBe("20");
    const line = new URL(codeHref("/repo", { path: "src/a.ts", line: 12, endLine: 12 }, "/r/abc"), "http://x");
    expect(line.searchParams.has("end")).toBe(false);
  });
});
