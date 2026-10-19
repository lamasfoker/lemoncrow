/**
 * Review UX gate (Phase 14 items 7 and 32).
 *
 * Every Review capability declares its primary surface here before it ships.
 * The tests in reviewUxGate.test.ts, ReaderHeader.test.tsx and
 * ReviewReader.test.tsx fail when:
 *
 * - a header control renders that is not listed in REVIEW_HEADER_CONTROLS
 *   (a new persistent header control needs an explicit UX review: add it here
 *   with the reason the palette, target menu and More are insufficient);
 * - more than REVIEW_MAX_PERSISTENT_VIEWS persistent views exist;
 * - an expert action is homed in the header without a header justification
 *   (placement order: command palette -> target context menu -> More ->
 *   header only with justification);
 * - an action moved out of the header in Phase 8 is missing from the command
 *   palette.
 *
 * The Reader's runtime invariants (one right rail, one blocking banner, one
 * status authority, one primary CTA) are asserted against the rendered Reader.
 */

export type ReviewSurfaceHome =
  | "brief"
  | "code"
  | "map"
  | "guided-review"
  | "status"
  | "comments"
  | "context"
  | "ask"
  | "activity"
  | "history"
  | "palette"
  | "target-menu"
  | "more"
  | "header"
  | "inbox"
  | "settings";

/** The only homes an expert/power action may take without a header justification, in placement order. */
export const EXPERT_ACTION_PLACEMENT_ORDER: readonly ReviewSurfaceHome[] = ["palette", "target-menu", "more"];

/** Persistent top-level Review categories. A Change may earn fewer; never more. */
export const REVIEW_PERSISTENT_VIEW_LABELS = ["Brief", "Code", "Map"] as const;
export const REVIEW_MAX_PERSISTENT_VIEWS = 3;

export interface ReviewHeaderControl {
  id: string;
  /** Matches the control's accessible name (aria-label, else text). */
  name: RegExp;
  /** Why this needs permanent header chrome rather than the palette, a target menu or More. */
  why: string;
  /** When it renders at all; absent means always. */
  when?: string;
}

export const REVIEW_HEADER_CONTROLS: readonly ReviewHeaderControl[] = [
  { id: "back", name: /^(Back to |Back$|All reviews)/, why: "Leaving or returning is the first navigation decision." },
  { id: "change-title", name: /^Open change overview$/, why: "The Change identity; opens the Brief." },
  { id: "snapshot", name: /open Snapshot history and compare$/, why: "Exact Snapshot identity stays beside the Change identity.", when: "desktop; phones use More and the palette" },
  { id: "work-item", name: /^Open associated work item /, why: "Identity context, not an action.", when: "a linked work item exists" },
  { id: "views", name: /^(Brief|Code|Map)$/, why: "The persistent categories (at most three)." },
  { id: "activity", name: /^Activity: /, why: "Promoted only while agents work or newer work arrived after the Snapshot.", when: "live or newer development" },
  { id: "comments", name: /^Review comments: /, why: "Review-wide unresolved threads." },
  { id: "status", name: /^Review Status: /, why: "The single readiness/progress authority." },
  { id: "ask", name: /^Ask about this review$/, why: "Right-rail mode; the other rail mode is Context.", when: "the composition offers Ask" },
  { id: "new-snapshot", name: /^(Capture current source|New Snapshot options)$/, why: "Source moved after the frozen Snapshot: it changes the next action.", when: "local source changed" },
  { id: "primary", name: /.*/, why: "The one dynamic primary-action slot." },
  { id: "handoff-destination", name: /^Change where fixes go$/, why: "Part of the primary feedback action.", when: "feedback can be handed off" },
  { id: "more", name: /^Review actions$/, why: "The More menu itself." },
  { id: "hosted-people", name: /^Review participants: /, why: "Hosted collaboration: who else reviews.", when: "hosted" },
  { id: "hosted-provider", name: /^External provider review: /, why: "Hosted: provider-owned review state.", when: "hosted with a provider" },
];

export interface ReviewCapabilityPlacement {
  id: string;
  /** What the reviewer gets, said plainly. */
  capability: string;
  home: ReviewSurfaceHome;
  secondary?: readonly ReviewSurfaceHome[];
  /** An expert/power action: palette -> target menu -> More, header only with justification. */
  expert?: boolean;
  /** Required when an expert action is homed in the header. */
  headerJustification?: string;
  /** Removed from the header during Phase 8: the command palette must still reach it. */
  movedFromHeader?: { phase: "8"; paletteId: string; paletteLabel: RegExp };
  /** The REVIEW_HEADER_CONTROLS entry it renders through, when the header is a home. */
  headerControl?: string;
  /** The plan package that introduced it, when recent. */
  origin?: string;
}

export const REVIEW_CAPABILITIES: readonly ReviewCapabilityPlacement[] = [
  { id: "brief", capability: "Change summary, cohorts and where to look", home: "brief", secondary: ["header", "palette"], headerControl: "views" },
  { id: "code", capability: "Exact Snapshot code, target by target", home: "code", secondary: ["header"], headerControl: "views" },
  { id: "map", capability: "Architecture and impact when material", home: "map", secondary: ["header", "palette", "context"], headerControl: "views", movedFromHeader: { phase: "8", paletteId: "map", paletteLabel: /^Show Map$/ } },
  { id: "guided-review", capability: "Guided review through cohorts", home: "guided-review", secondary: ["brief", "palette"], movedFromHeader: { phase: "8", paletteId: "guided-review", paletteLabel: /^Start guided review$/ } },
  { id: "review-status", capability: "Progress, blockers, checks and readiness", home: "status", secondary: ["header", "palette"], movedFromHeader: { phase: "8", paletteId: "status", paletteLabel: /^Open Review Status$/ }, headerControl: "status" },
  { id: "activity", capability: "Development and judgment history", home: "activity", secondary: ["header", "context", "palette"], headerControl: "activity", movedFromHeader: { phase: "8", paletteId: "development", paletteLabel: /^Open Activity$/ } },
  { id: "comments", capability: "Review-wide unresolved threads", home: "comments", secondary: ["header", "palette"], headerControl: "comments" },
  { id: "ask", capability: "Ask about the change", home: "ask", secondary: ["header", "palette", "target-menu"], headerControl: "ask" },
  { id: "ask-provider", capability: "Ask provider and model choice", home: "ask", expert: true, secondary: [] },
  { id: "context", capability: "Impact, checks, discussion and activity for one target", home: "context", secondary: ["target-menu", "palette", "more"] },
  { id: "code-peek", capability: "Definition and usages without losing position", home: "context", secondary: ["target-menu"] },
  { id: "history", capability: "Snapshot history and comparison", home: "history", secondary: ["header", "more", "palette"], headerControl: "snapshot" },
  { id: "finish", capability: "Finish review with a verdict", home: "header", secondary: ["more", "palette"], headerJustification: "It is the primary action when review work is done.", headerControl: "primary" },
  { id: "send-feedback", capability: "Send feedback to the author or agent", home: "header", secondary: ["palette"], headerJustification: "It is the primary action while feedback is unsent.", headerControl: "primary" },
  { id: "merge-when-ready", capability: "Merge when ready (provider merge)", home: "status", secondary: ["header"], origin: "P10-LAND", headerControl: "primary" },
  { id: "merge-readiness", capability: "Merge requirements and blockers", home: "status", origin: "P10-LAND" },
  { id: "inbox-blocking-actions", capability: "Exact blocking action per Review", home: "inbox", origin: "P10-LAND" },
  { id: "start-review", capability: "Start the first Review from an empty hosted Inbox", home: "inbox", origin: "P14-UX" },
  { id: "deployments", capability: "Deployment state of the merged Change", home: "status", origin: "P15-DEPLOY" },
  { id: "production-outcomes", capability: "Predicted vs observed production behavior", home: "status", secondary: ["brief"], origin: "P15-OBS" },
  { id: "automation", capability: "Automation rules and authority", home: "status", expert: true, secondary: [] },
  { id: "review-record", capability: "Signed Review record and export", home: "status", expert: true, secondary: [] },
  { id: "premerge-checks", capability: "Pre-merge checks, waivers and re-binding", home: "status", expert: true, secondary: [] },
  { id: "reviewer-suggestions", capability: "Suggested reviewers", home: "status" },
  { id: "corrections", capability: "Fix all, CI fix, tests, conflicts, revert and fix-forward", home: "palette", expert: true, secondary: ["more", "target-menu"], origin: "P12" },
  { id: "diff-style", capability: "Split / unified diff", home: "palette", expert: true, secondary: ["more"] },
  { id: "line-wrap", capability: "Wrap long code lines", home: "palette", expert: true, secondary: ["more"] },
  { id: "code-theme", capability: "Code theme", home: "more", expert: true },
  { id: "focus-mode", capability: "Focus mode", home: "palette", expert: true, secondary: ["more"] },
  { id: "order", capability: "Recommended or file order", home: "palette", expert: true, secondary: ["code"] },
  { id: "search", capability: "Search review areas", home: "code", secondary: ["palette"] },
  { id: "bulk-review", capability: "Mark a whole file reviewed, with Undo", home: "target-menu", expert: true },
  { id: "finding-disposition", capability: "Confirm, dismiss or fix an AI finding", home: "code", secondary: ["context"] },
  { id: "download-patch", capability: "Download the Snapshot patch", home: "more", expert: true },
  { id: "copy-ids", capability: "Copy Change ID / workspace path", home: "more", expert: true },
  { id: "publish-github", capability: "Publish to GitHub", home: "more", expert: true },
  { id: "capture-snapshot", capability: "Capture newer local source as a Snapshot", home: "header", secondary: ["more", "palette"], headerJustification: "Shown only when source moved; it changes the next action.", headerControl: "new-snapshot" },
  { id: "view-options", capability: "Brief detail level", home: "brief", expert: true, secondary: [] },
  { id: "product-feedback", capability: "Make a wish", home: "more" },
];

const HOMES: ReadonlySet<ReviewSurfaceHome> = new Set<ReviewSurfaceHome>([
  "brief", "code", "map", "guided-review", "status", "comments", "context", "ask", "activity", "history",
  "palette", "target-menu", "more", "header", "inbox", "settings",
]);

/** Static gate over a placement registry; [] means the registry passes. */
export function reviewUxGateViolations(
  capabilities: readonly ReviewCapabilityPlacement[] = REVIEW_CAPABILITIES,
  headerControls: readonly ReviewHeaderControl[] = REVIEW_HEADER_CONTROLS,
  persistentViews: readonly string[] = REVIEW_PERSISTENT_VIEW_LABELS,
): string[] {
  const violations: string[] = [];
  const seen = new Set<string>();
  if (persistentViews.length > REVIEW_MAX_PERSISTENT_VIEWS) {
    violations.push(`${persistentViews.length} persistent views exceed the budget of ${REVIEW_MAX_PERSISTENT_VIEWS}`);
  }
  for (const control of headerControls) {
    if (!control.why.trim()) violations.push(`header control ${control.id} has no justification`);
  }
  for (const entry of capabilities) {
    if (seen.has(entry.id)) violations.push(`${entry.id} is declared twice`);
    seen.add(entry.id);
    if (!HOMES.has(entry.home)) violations.push(`${entry.id} has no known primary home`);
    if (!entry.capability.trim()) violations.push(`${entry.id} does not say what it does`);
    if (entry.home === "header" && !entry.headerJustification?.trim()) {
      violations.push(`${entry.id} takes header chrome without a justification`);
    }
    // Placement order for expert actions: palette -> target menu -> More; the
    // header only with a justification AND a palette/target-menu/More home too.
    if (entry.expert && entry.home === "header" && !EXPERT_ACTION_PLACEMENT_ORDER.some((home) => entry.secondary?.includes(home))) {
      violations.push(`expert action ${entry.id} is in the header without a palette, target-menu or More home`);
    }
    if (entry.expert && (entry.secondary ?? []).includes("header")) {
      violations.push(`expert action ${entry.id} adds header chrome as a secondary entry`);
    }
    const inHeader = entry.home === "header" || (entry.secondary ?? []).includes("header");
    if (inHeader && !headerControls.some((control) => control.id === entry.headerControl)) {
      violations.push(`${entry.id} renders in the header without a registered header control`);
    }
    if (entry.movedFromHeader && !entry.movedFromHeader.paletteId) {
      violations.push(`${entry.id} moved out of the header without a palette action`);
    }
  }
  return violations;
}

/** The registered header control an accessible name belongs to, or null (the gate fails). */
export function registeredHeaderControl(
  name: string,
  options: { primary?: boolean } = {},
  headerControls: readonly ReviewHeaderControl[] = REVIEW_HEADER_CONTROLS,
): ReviewHeaderControl | null {
  if (options.primary) return headerControls.find((control) => control.id === "primary") ?? null;
  return headerControls.find((control) => control.id !== "primary" && control.name.test(name)) ?? null;
}

/** Palette actions the Phase 8 header removals require. */
export function requiredPaletteCoverage(
  capabilities: readonly ReviewCapabilityPlacement[] = REVIEW_CAPABILITIES,
): { id: string; paletteId: string; paletteLabel: RegExp }[] {
  return capabilities
    .filter((entry) => entry.movedFromHeader)
    .map((entry) => ({ id: entry.id, paletteId: entry.movedFromHeader!.paletteId, paletteLabel: entry.movedFromHeader!.paletteLabel }));
}
