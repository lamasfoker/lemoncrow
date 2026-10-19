import type {
  BackgroundBudgetStatus,
  IntelligenceRouteProjection,
} from "../components/IntelligenceSettings";

/** Wire fixtures of BI0's route projection and BI1's envelope status, shared by the Settings tests. */
export const origin = {
  layer: "built_in_default",
  source: "intelligence.driver",
};

export function routeFixture(
  overrides: Partial<IntelligenceRouteProjection> = {}
): IntelligenceRouteProjection {
  return {
    status: "ready",
    reason: "",
    status_note:
      "ready means a model route is configured; provider credentials are checked on the first model call",
    requested: {
      driver: "auto",
      model: "auto",
      driver_origin: origin,
      model_origin: origin,
    },
    resolved: {
      driver: "openai",
      model: "gpt-4o-mini",
      model_source: "pinned",
      driver_origin: origin,
      model_origin: origin,
    },
    run_on: {
      effective_value: "local",
      origin,
      offered_here: true,
      note: "governs background work",
    },
    background_enabled: true,
    background_note: "governs background work: automatic AI Review today",
    data_locus: null,
    policy: { valid: true, errors: [] },
    editable: false,
    edit_source:
      "this machine's LemonCrow settings: lemoncrow settings set <key> <value>",
    ...overrides,
  };
}

export function budgetFixture(
  overrides: Partial<Extract<BackgroundBudgetStatus, { available: true }>> = {}
): BackgroundBudgetStatus {
  return {
    available: true,
    enabled: true,
    state: "running",
    reason: "admitted",
    period: {
      kind: "month",
      start_unix_s: 1_790_000_000,
      end_unix_s: 1_792_600_000,
    },
    policy: {
      enabled: true,
      monthly_model_calls: 200,
      daily_model_calls: null,
      monthly_managed_usd: 25,
      valid: true,
      configured: true,
    },
    background: {
      model_calls_used: 61,
      model_calls_held: 0,
      model_calls_remaining: 139,
      today_model_calls: 4,
      today_model_calls_remaining: null,
      cost_complete: true,
      managed_usd_used: 8.42,
      measured_managed_usd: 8.42,
      unmeasured_cost_calls: 0,
      managed_usd_held: 0,
      managed_usd_remaining: 16.58,
    },
    managed_spend: {
      applies: true,
      enforced: true,
      note: "the dollar ceiling binds LemonCrow-managed execution",
    },
    priorities: [
      {
        priority: "review_auto",
        share_percent: 100,
        state: "running",
        reason: "admitted",
      },
    ],
    automatic_review: {
      envelope_enforced: true,
      note: "automatic Review is metered and limited by this envelope",
    },
    by_capability: [
      {
        lane: "background",
        priority: "review_auto",
        capability: "review.auto_ai",
        admissions: 12,
        model_calls_used: 61,
        model_calls_held: 0,
        cost_complete: true,
        managed_usd_used: 8.42,
        measured_managed_usd: 8.42,
        unmeasured_cost_calls: 0,
      },
    ],
    ...overrides,
  };
}
