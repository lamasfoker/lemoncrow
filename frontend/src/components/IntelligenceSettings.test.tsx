import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import {
  budgetFixture,
  origin,
  routeFixture,
} from "../test/intelligenceFixtures";
import {
  BackgroundBudgetPanel,
  IntelligenceOverview,
} from "./IntelligenceSettings";

function renderOverview(
  props: Partial<Parameters<typeof IntelligenceOverview>[0]> = {}
) {
  return render(
    <MemoryRouter>
      <IntelligenceOverview
        scope="hosted"
        route={routeFixture({
          run_on: {
            effective_value: "managed",
            origin,
            offered_here: true,
            note: "",
          },
        })}
        background={budgetFixture()}
        lifetime="Hosted workers run background work from a durable queue."
        budgetEditSource="the deployment's quota policy file"
        {...props}
      />
    </MemoryRouter>
  );
}

function valueOf(label: string): string {
  const term = screen.getAllByText(label, { selector: "dt" })[0];
  return term.parentElement?.querySelector("dd")?.textContent ?? "";
}

describe("IntelligenceOverview", () => {
  it("shows the default product view without internal ids or editable controls", () => {
    renderOverview();
    expect(valueOf("AI route")).toBe("Auto · OpenAI-compatible API");
    expect(valueOf("Model")).toBe("Auto · gpt-4o-mini");
    expect(valueOf("Runs on")).toBe("LemonCrow-managed");
    expect(valueOf("Background intelligence")).toBe("On");
    expect(valueOf("Monthly budget")).toBe("200 calls / $25.00 managed");
    expect(valueOf("Usage this month")).toBe("61 calls · $8.42");
    expect(valueOf("Status")).toBe("Running");
    expect(screen.getByTestId("intelligence-edit-source")).toHaveTextContent(
      "Read-only here"
    );
    expect(screen.queryByTestId("background-paused")).not.toBeInTheDocument();
    // No fake writeability: nothing on the page is a control besides disclosure.
    for (const role of [
      "button",
      "textbox",
      "combobox",
      "checkbox",
      "switch",
      "spinbutton",
    ]) {
      expect(screen.queryAllByRole(role)).toHaveLength(0);
    }
    // Capability ids stay out of the default view (labels only, inside Advanced).
    expect(screen.queryByText(/review\.auto_ai/)).not.toBeInTheDocument();
  });

  it("shows exhaustion as one durable paused state with the way forward", () => {
    renderOverview({
      background: budgetFixture({
        state: "paused",
        reason: "background_exhausted",
      }),
      usageHref: "/settings/usage",
    });
    const notices = screen.getAllByTestId("background-paused");
    expect(notices).toHaveLength(1);
    expect(notices[0]).toHaveTextContent(
      "Background intelligence paused · monthly budget reached"
    );
    expect(notices[0]).toHaveTextContent(
      "Current Reviews still run deterministic checks and remain reviewable."
    );
    expect(
      within(notices[0]).getByRole("link", { name: "Usage" })
    ).toHaveAttribute("href", "/settings/usage");
    expect(notices[0]).toHaveTextContent(
      "Change the budget in the deployment's quota policy file."
    );
    expect(valueOf("Status")).toBe("Pausedmonthly budget reached");
  });

  it.each([
    [
      "limited",
      "priority_reserved",
      "Running · limitedthe rest of the budget is held for higher-priority work",
    ],
    ["disabled", "background_disabled", "Offbackground intelligence is off"],
  ] as const)(
    "keeps the paused notice for paused alone (%s)",
    (state, reason, shown) => {
      renderOverview({ background: budgetFixture({ state, reason }) });
      expect(screen.queryByTestId("background-paused")).not.toBeInTheDocument();
      expect(valueOf("Status")).toBe(shown);
    }
  );

  it("never claims a route that will not run", () => {
    renderOverview({
      route: routeFixture({
        status: "not_configured",
        reason: "no model route is configured",
      }),
    });
    expect(valueOf("AI route")).toBe(
      "Not configuredno model route is configured"
    );
    expect(valueOf("Model")).toBe("Auto");
    expect(valueOf("Status")).toContain("Waiting for an AI provider");
  });

  it("says an unoffered locus refuses automatic work instead of moving it", () => {
    renderOverview({
      route: routeFixture({
        run_on: {
          effective_value: "managed",
          origin,
          offered_here: false,
          note: "",
        },
      }),
    });
    expect(valueOf("Runs on")).toContain("refused, never moved elsewhere");
  });

  it("labels unknown spend as a lower bound", () => {
    const status = budgetFixture();
    if (!status.available) throw new Error("fixture");
    renderOverview({
      background: {
        ...status,
        background: {
          ...status.background,
          cost_complete: false,
          managed_usd_used: null,
          measured_managed_usd: 3,
          unmeasured_cost_calls: 2,
        },
      },
    });
    expect(valueOf("Usage this month")).toContain("at least $3.00");
    expect(valueOf("Usage this month")).toContain(
      "2 calls reported no cost; spend shown is a lower bound."
    );
  });

  it("keeps local copy local: no dollar ceiling and no hosted-only promises", () => {
    renderOverview({
      scope: "local",
      route: routeFixture(),
      background: budgetFixture({
        managed_spend: {
          applies: false,
          enforced: false,
          note: "no LemonCrow-managed execution here",
        },
      }),
      lifetime:
        "Background work runs only while LemonCrow is running on this machine.",
    });
    expect(valueOf("Runs on")).toBe("This machine");
    expect(valueOf("Monthly budget")).toBe("200 calls");
    expect(
      screen.getByText(/only while LemonCrow is running/)
    ).toBeInTheDocument();
    expect(screen.queryByText(/Explicit requests/)).not.toBeInTheDocument();
  });

  it("never calls absent spend a lower bound", () => {
    const status = budgetFixture({
      managed_spend: {
        applies: false,
        enforced: false,
        note: "no LemonCrow-managed execution here",
      },
    });
    if (!status.available) throw new Error("fixture");
    renderOverview({
      scope: "local",
      background: {
        ...status,
        background: {
          ...status.background,
          cost_complete: false,
          unmeasured_cost_calls: 1,
        },
      },
    });
    expect(valueOf("Usage this month")).toContain("1 call reported no cost.");
    expect(valueOf("Usage this month")).not.toContain("lower bound");
    expect(valueOf("Usage this month")).not.toContain("$");
  });

  it("shows the UTC month with its last day, not the next month's first", () => {
    // October 2026 in UTC: the period ends at the first instant of November.
    renderOverview({
      background: budgetFixture({
        period: {
          kind: "month",
          start_unix_s: 1_790_812_800,
          end_unix_s: 1_793_491_200,
        },
      }),
    });
    const day = (y: number, m: number, d: number) =>
      new Date(Date.UTC(y, m, d)).toLocaleDateString(undefined, {
        timeZone: "UTC",
      });
    expect(valueOf("Period")).toBe(
      `${day(2026, 9, 1)} – ${day(2026, 9, 31)} (UTC)`
    );
  });
});

describe("BackgroundBudgetPanel", () => {
  it("says when the budget is unavailable or still loading", () => {
    const { rerender } = render(
      <BackgroundBudgetPanel
        status={{
          available: false,
          reason: "no PostgreSQL quota authority holds the background budget",
        }}
        scope="hosted"
        editSource="x"
      />
    );
    expect(screen.getByTestId("background-budget")).toHaveTextContent(
      "Unavailable"
    );
    expect(screen.getByTestId("background-budget")).toHaveTextContent(
      "no PostgreSQL quota authority"
    );
    rerender(
      <BackgroundBudgetPanel status={null} scope="hosted" editSource="x" />
    );
    expect(screen.getByTestId("background-budget")).toHaveTextContent(
      "Loading…"
    );
  });

  it("lists capability activity by product name under Advanced", () => {
    render(
      <BackgroundBudgetPanel
        status={budgetFixture()}
        scope="hosted"
        editSource="x"
      />
    );
    const table = screen.getByRole("table", {
      name: "Background work this month by capability",
    });
    expect(
      within(table).getByRole("rowheader", { name: "Automatic AI Review" })
    ).toBeInTheDocument();
    expect(
      screen.getByText(/never stop because this budget is used up/)
    ).toBeInTheDocument();
  });
});
