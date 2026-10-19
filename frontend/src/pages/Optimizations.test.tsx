import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const range = vi.hoisted(() => ({ days: 7, range: "7d" }));
vi.mock("../lib/TimeRangeContext", () => ({
  useTimeRange: () => range,
}));
vi.mock("../api", async (importOriginal) => {
  const original = await importOriginal<typeof import("../api")>();
  return {
    ...original,
    api: {
      ...original.api,
      optimizationsSummary: vi.fn(),
      outcomesSummary: vi.fn(() => Promise.resolve(null)),
    },
  };
});

import { api } from "../api";
import Optimizations from "./Optimizations";

describe("Optimizations", () => {
  afterEach(() => {
    range.days = 7;
    range.range = "7d";
    vi.clearAllMocks();
  });

  it("ignores a slow response for a window the reader already left", async () => {
    let rejectFirst: (error: Error) => void = () => {};
    const optimizationsSummary = vi.mocked(api.optimizationsSummary);
    optimizationsSummary.mockImplementationOnce(
      () => new Promise((_, reject) => { rejectFirst = reject; }),
    );
    optimizationsSummary.mockImplementationOnce(() => Promise.reject(new Error("newest window failed")));

    const view = render(<Optimizations />);
    range.days = 30;
    range.range = "30d";
    view.rerender(<Optimizations />);
    expect(await screen.findByRole("alert")).toHaveTextContent("newest window failed");

    await act(async () => {
      rejectFirst(new Error("stale window failed"));
    });

    expect(screen.getByRole("alert")).toHaveTextContent("newest window failed");
  });
});
