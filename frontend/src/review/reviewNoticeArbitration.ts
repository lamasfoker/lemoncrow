export type ReviewBlockingNoticeKind = "error" | "comparison" | "revision-delta" | null;

export interface ReviewBlockingNoticeState {
  hasError: boolean;
  comparisonActive: boolean;
  revisionDeltaActive: boolean;
}

/**
 * One blocking row above the Reader. The order is deliberate product policy:
 * a failed operation needs recovery first; explicit comparison owns the frame
 * next; a routine Snapshot delta is informational and comes last.
 */
export function reviewBlockingNoticeKind(state: ReviewBlockingNoticeState): ReviewBlockingNoticeKind {
  if (state.hasError) return "error";
  if (state.comparisonActive) return "comparison";
  if (state.revisionDeltaActive) return "revision-delta";
  return null;
}

const CONTEXT_SIGNAL_PRIORITY: Record<string, number> = {
  "revision-advanced": 10,
  "running-draft": 20,
  historical: 30,
  attention: 40,
  hosted: 45,
  "context-failures": 50,
  "capabilities-fallback": 55,
  "capabilities-unsupported": 56,
  draft: 60,
  "fix-of": 70,
  "bulk-undo": 80,
  preparation: 90,
};

export function reviewContextSignalPriority(id: string): number {
  return CONTEXT_SIGNAL_PRIORITY[id] ?? 100;
}
