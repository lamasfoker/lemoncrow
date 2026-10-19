import { describe, expect, it } from "vitest";

import {
  REVIEW_CAPABILITIES,
  REVIEW_HEADER_CONTROLS,
  REVIEW_MAX_PERSISTENT_VIEWS,
  REVIEW_PERSISTENT_VIEW_LABELS,
  registeredHeaderControl,
  requiredPaletteCoverage,
  reviewUxGateViolations,
  type ReviewCapabilityPlacement,
} from "./reviewUxGate";

describe("Review UX gate", () => {
  it("the current placement registry passes", () => {
    expect(reviewUxGateViolations()).toEqual([]);
    expect(REVIEW_PERSISTENT_VIEW_LABELS.length).toBeLessThanOrEqual(REVIEW_MAX_PERSISTENT_VIEWS);
  });

  it("declares recent Review surfaces (P10-LAND, P15-OBS) in existing homes, never a new view", () => {
    const byId = new Map(REVIEW_CAPABILITIES.map((entry) => [entry.id, entry]));
    expect(byId.get("merge-when-ready")?.home).toBe("status");
    expect(byId.get("merge-readiness")?.home).toBe("status");
    expect(byId.get("inbox-blocking-actions")?.home).toBe("inbox");
    expect(byId.get("production-outcomes")).toMatchObject({ home: "status", secondary: ["brief"], origin: "P15-OBS" });
  });

  it("requires palette coverage for every Phase 8 header removal", () => {
    expect(requiredPaletteCoverage().map((item) => item.paletteId).sort()).toEqual(["development", "guided-review", "map", "status"]);
  });

  it("rejects a header control or expert action that skips review", () => {
    const expertInHeader: ReviewCapabilityPlacement = { id: "x", capability: "Run deep review", home: "header", expert: true, headerControl: "primary" };
    expect(reviewUxGateViolations([expertInHeader])).toEqual([
      "x takes header chrome without a justification",
      "expert action x is in the header without a palette, target-menu or More home",
    ]);
    const sneaky: ReviewCapabilityPlacement = { id: "y", capability: "Run plan", home: "status", expert: true, secondary: ["header"], headerControl: "status" };
    expect(reviewUxGateViolations([sneaky])).toEqual(["expert action y adds header chrome as a secondary entry"]);
    const unregistered: ReviewCapabilityPlacement = { id: "z", capability: "Thing", home: "comments", secondary: ["header"] };
    expect(reviewUxGateViolations([unregistered])).toEqual(["z renders in the header without a registered header control"]);
    expect(reviewUxGateViolations([], REVIEW_HEADER_CONTROLS, ["Brief", "Tour", "Code", "Map"])).toEqual([
      "4 persistent views exceed the budget of 3",
    ]);
    const moved: ReviewCapabilityPlacement = { id: "t", capability: "Tour", home: "guided-review", movedFromHeader: { phase: "8", paletteId: "", paletteLabel: /x/ } };
    expect(reviewUxGateViolations([moved])).toEqual(["t moved out of the header without a palette action"]);
  });

  it("rejects a header control with no justification and a capability declared twice", () => {
    const unjustified = [...REVIEW_HEADER_CONTROLS, { id: "deep-review", name: /^Run deeper review$/, why: "  " }];
    expect(reviewUxGateViolations([], unjustified)).toEqual(["header control deep-review has no justification"]);
    const once: ReviewCapabilityPlacement = { id: "twice", capability: "Thing", home: "comments" };
    expect(reviewUxGateViolations([once])).toEqual([]);
    expect(reviewUxGateViolations([once, { ...once, home: "more" }])).toEqual(["twice is declared twice"]);
  });

  it("maps rendered header names to registered controls and nothing else", () => {
    expect(registeredHeaderControl("Review Status: Ready")?.id).toBe("status");
    expect(registeredHeaderControl("Map")?.id).toBe("views");
    expect(registeredHeaderControl("Tour")).toBeNull();
    expect(registeredHeaderControl("Run deeper review")).toBeNull();
    expect(registeredHeaderControl("anything", { primary: true })?.id).toBe("primary");
  });
});
