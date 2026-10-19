import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import Analytics from "./Analytics";
import { api, ApiError } from "../api";
import { TimeRangeProvider } from "../lib/TimeRangeContext";

describe("Usage analytics when telemetry is unavailable", () => {
  afterEach(() => vi.restoreAllMocks());

  it("shows the server's reason instead of a raw ApiError string", async () => {
    const reason = "Agent telemetry is not connected for this organization.";
    vi.spyOn(api, "granularAnalytics").mockRejectedValue(new ApiError(501, reason));
    vi.spyOn(api, "analyticsDashboard").mockRejectedValue(new ApiError(501, reason));

    render(
      <MemoryRouter>
        <TimeRangeProvider>
          <Analytics />
        </TimeRangeProvider>
      </MemoryRouter>,
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(reason);
    expect(alert).not.toHaveTextContent("ApiError");
    expect(alert).not.toHaveTextContent("Error:");
  });
});
