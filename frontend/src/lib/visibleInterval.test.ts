import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { setVisibleInterval } from "./visibleInterval";

let visibility: DocumentVisibilityState = "visible";

function setVisibility(next: DocumentVisibilityState) {
  visibility = next;
  document.dispatchEvent(new Event("visibilitychange"));
}

beforeEach(() => {
  vi.useFakeTimers();
  visibility = "visible";
  vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility);
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

it("pauses while hidden and catches up once visible again", () => {
  const tick = vi.fn();
  const stop = setVisibleInterval(tick, 1000);
  vi.advanceTimersByTime(2000);
  expect(tick).toHaveBeenCalledTimes(2);

  setVisibility("hidden");
  vi.advanceTimersByTime(5000);
  expect(tick).toHaveBeenCalledTimes(2);

  setVisibility("visible");
  expect(tick).toHaveBeenCalledTimes(3);

  stop();
  vi.advanceTimersByTime(5000);
  setVisibility("hidden");
  setVisibility("visible");
  expect(tick).toHaveBeenCalledTimes(3);
});

it("does not refire on a visibility change without a missed tick", () => {
  const tick = vi.fn();
  const stop = setVisibleInterval(tick, 1000);
  setVisibility("hidden");
  setVisibility("visible");
  expect(tick).not.toHaveBeenCalled();
  stop();
});

it("skips ticks while the previous promise is pending, even if it rejects", async () => {
  let settle: (ok: boolean) => void = () => undefined;
  const tick = vi.fn(
    () =>
      new Promise<void>((resolve, reject) => {
        settle = (ok) => (ok ? resolve() : reject(new Error("boom")));
      })
  );
  const stop = setVisibleInterval(tick, 1000);
  vi.advanceTimersByTime(3000);
  expect(tick).toHaveBeenCalledTimes(1);

  settle(false);
  await vi.advanceTimersByTimeAsync(1000);
  expect(tick).toHaveBeenCalledTimes(2);

  settle(true);
  await vi.advanceTimersByTimeAsync(1000);
  expect(tick).toHaveBeenCalledTimes(3);
  stop();
});
