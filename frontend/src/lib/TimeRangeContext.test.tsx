import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TimeRangeProvider, useTimeRange } from "./TimeRangeContext";

function Probe() {
  const { range } = useTimeRange();
  return <div>range:{range}</div>;
}

describe("TimeRangeProvider", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders with the default range when browser storage access throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });

    render(
      <TimeRangeProvider>
        <Probe />
      </TimeRangeProvider>,
    );

    expect(screen.getByText("range:7d")).toBeInTheDocument();
  });
});
