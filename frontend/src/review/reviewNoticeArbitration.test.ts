import { describe, expect, it } from "vitest";

import { reviewBlockingNoticeKind, reviewContextSignalPriority } from "./reviewNoticeArbitration";

describe("review notice arbitration", () => {
  it("allows at most one blocking notice with deterministic priority", () => {
    expect(reviewBlockingNoticeKind({
      hasError: true,
      comparisonActive: true,
      revisionDeltaActive: true,
    })).toBe("error");
    expect(reviewBlockingNoticeKind({
      hasError: false,
      comparisonActive: true,
      revisionDeltaActive: true,
    })).toBe("comparison");
    expect(reviewBlockingNoticeKind({
      hasError: false,
      comparisonActive: false,
      revisionDeltaActive: true,
    })).toBe("revision-delta");
    expect(reviewBlockingNoticeKind({
      hasError: false,
      comparisonActive: false,
      revisionDeltaActive: false,
    })).toBeNull();
  });

  it("orders invalidating context ahead of degradation and ephemeral acknowledgements", () => {
    const ids = [
      "preparation",
      "bulk-undo",
      "fix-of",
      "capabilities-unsupported",
      "context-failures",
      "historical",
      "revision-advanced",
    ];
    expect(ids.sort((a, b) => reviewContextSignalPriority(a) - reviewContextSignalPriority(b))).toEqual([
      "revision-advanced",
      "historical",
      "context-failures",
      "capabilities-unsupported",
      "fix-of",
      "bulk-undo",
      "preparation",
    ]);
  });
});
