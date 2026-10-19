import { describe, expect, it, vi } from "vitest";

import { transientRequestFailure, withTransientRetry } from "./transientRetry";

const status = (code: number) => Object.assign(new Error(String(code)), { status: code });

describe("transient request retry", () => {
  it("treats a missing HTTP response and transient statuses as retryable", () => {
    expect(transientRequestFailure(new TypeError("Failed to fetch"))).toBe(true);
    expect(transientRequestFailure(status(503))).toBe(true);
    expect(transientRequestFailure(status(429))).toBe(true);
    expect(transientRequestFailure(status(404))).toBe(false);
    expect(transientRequestFailure(status(403))).toBe(false);
  });

  it("recovers from a dropped connection", async () => {
    const run = vi.fn()
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce("loaded");
    await expect(withTransientRetry(run, [0, 0])).resolves.toBe("loaded");
    expect(run).toHaveBeenCalledTimes(2);
  });

  it("stops after the last delay and never retries a permanent refusal", async () => {
    const down = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(withTransientRetry(down, [0, 0])).rejects.toThrow("Failed to fetch");
    expect(down).toHaveBeenCalledTimes(3);

    const missing = vi.fn().mockRejectedValue(status(404));
    await expect(withTransientRetry(missing, [0, 0])).rejects.toThrow("404");
    expect(missing).toHaveBeenCalledTimes(1);
  });
});
