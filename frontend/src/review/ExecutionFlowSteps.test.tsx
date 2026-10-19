import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import ExecutionFlowSteps from "./ExecutionFlowSteps";
import type { ReviewArchitectureDelta } from "./types";

function flow(observed: ReviewArchitectureDelta["flow"]["observed"]): ReviewArchitectureDelta["flow"] {
  return {
    applicable: true,
    kind: "changed_relationship_flow",
    reason: "",
    steps: [
      {
        edge_id: "e1",
        from: "api/app.py",
        to: "core/engine.py",
        relationship: "references",
        delta_status: "added",
        base_weight: 0,
        snapshot_weight: 1,
        grounding: "observed",
        trace: { side: "head", caller_fn: "main", callee_fn: "run", recipe: "unit", evidence_ref: "check:test:runtime-observation:h", record_id: "ev-1" },
      },
      {
        edge_id: "e2",
        from: "api/app.py",
        to: "store/db.py",
        relationship: "references",
        delta_status: "added",
        base_weight: 0,
        snapshot_weight: 1,
        grounding: "inferred",
        trace: null,
      },
    ],
    observed,
  };
}

const CALL = { from: "api/app.py", from_fn: "main", to: "core/engine.py", to_fn: "run", evidence_ref: "check:test:runtime-observation:h" };

describe("ExecutionFlowSteps", () => {
  it("labels observed steps with their trace and inferred steps as inferred", () => {
    render(
      <ExecutionFlowSteps
        flow={flow({ available: true, comparable: true, grounded_steps: 1, inferred_steps: 1, base: [], head: [CALL], added: [CALL], removed: [], note: "Observed steps cite call traces." })}
      />,
    );
    const observed = within(screen.getByTestId("flow-step-e1")).getByTestId("flow-grounding-observed");
    expect(observed).toHaveTextContent("observed · main → run");
    expect(observed).toHaveAttribute("title", expect.stringContaining("check:test:runtime-observation:h"));
    expect(within(screen.getByTestId("flow-step-e2")).getByTestId("flow-grounding-inferred")).toHaveTextContent("inferred");
    expect(within(screen.getByTestId("flow-step-e2")).queryByTestId("flow-grounding-observed")).toBeNull();
    const sequence = screen.getByTestId("observed-sequence");
    expect(sequence).toHaveTextContent("+ api/app.py:main → core/engine.py:run");
    expect(sequence).toHaveTextContent("Observed steps cite call traces.");
  });

  it("claims no added or removed call without a comparable pair, and every step is inferred without traces", () => {
    const { unmount } = render(
      <ExecutionFlowSteps
        flow={flow({ available: true, comparable: false, grounded_steps: 1, inferred_steps: 1, base: [], head: [CALL], added: [], removed: [], note: "n" })}
      />,
    );
    expect(screen.getByTestId("observed-sequence")).not.toHaveTextContent("+ ");
    expect(screen.getByTestId("observed-sequence")).toHaveTextContent("api/app.py:main → core/engine.py:run");
    unmount();
    const bare = flow(undefined);
    bare.steps = bare.steps.map((step) => ({ ...step, grounding: undefined, trace: undefined }));
    render(<ExecutionFlowSteps flow={bare} />);
    expect(screen.getAllByTestId("flow-grounding-inferred")).toHaveLength(2);
    expect(screen.getByText(/not a claim of runtime order/)).toBeInTheDocument();
  });
});
