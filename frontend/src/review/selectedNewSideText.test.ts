import { describe, expect, it } from "vitest";
import { selectedNewSideTextFromPatch } from "./selectedNewSideText";

describe("selectedNewSideTextFromPatch", () => {
  const patch = "--- a/src/file.py\n+++ b/src/file.py\n@@ -2,3 +2,4 @@\n context\n-old\n+first\n+second\n trailing\n@@ -20 +21 @@\n-later\n+later changed\n";

  it("uses new-side lines and keeps their line endings", () => {
    expect(selectedNewSideTextFromPatch(patch, 3, 4)).toBe("first\nsecond\n");
    expect(selectedNewSideTextFromPatch(patch, 2, 3)).toBe("context\nfirst\n");
    expect(selectedNewSideTextFromPatch(patch, 21, 21)).toBe("later changed\n");
  });

  it("falls back when the selected range crosses context absent from the patch", () => {
    expect(selectedNewSideTextFromPatch(patch, 5, 21)).toBeNull();
    expect(selectedNewSideTextFromPatch(patch, 1, 1)).toBeNull();
  });

  it("preserves a selected final line without a newline", () => {
    expect(selectedNewSideTextFromPatch("@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n", 1, 1)).toBe("new");
  });
});
