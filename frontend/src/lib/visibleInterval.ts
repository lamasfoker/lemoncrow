/**
 * `setInterval` for polling: ticks only while the page is visible, and skips a
 * tick while the previous one's promise is still pending, so a slow endpoint
 * never stacks requests. A tick missed while the tab was hidden runs as soon as
 * it is visible again, so the view is current when the user looks at it.
 * Returns the cleanup.
 */
export function setVisibleInterval(
  tick: () => unknown,
  intervalMs: number
): () => void {
  let inFlight = false;
  let missed = false;
  const done = () => {
    inFlight = false;
  };
  const run = () => {
    if (document.visibilityState === "hidden") {
      missed = true;
      return;
    }
    if (inFlight) return;
    missed = false;
    const result = tick();
    if (result instanceof Promise) {
      inFlight = true;
      result.then(done, done);
    }
  };
  const onVisibilityChange = () => {
    if (missed && document.visibilityState !== "hidden") run();
  };
  const id = window.setInterval(run, intervalMs);
  document.addEventListener("visibilitychange", onVisibilityChange);
  return () => {
    window.clearInterval(id);
    document.removeEventListener("visibilitychange", onVisibilityChange);
  };
}
