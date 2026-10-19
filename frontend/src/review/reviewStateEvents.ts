type ReviewEventFetch = (path: string, init?: RequestInit) => Promise<Response>;

/**
 * Statuses no reconnect can fix: authentication already failed after the
 * fetch's own recovery, access was revoked, or the Review is gone.
 */
const TERMINAL_STREAM_STATUSES = new Set([401, 403, 404, 410]);

/** Fetch preserves the local browser capability header, unlike EventSource. */
export function subscribeReviewState<T>(
  path: string,
  onState: (state: T) => void | Promise<void>,
  fetchEventStream: ReviewEventFetch,
  retryMs = 1000,
  onTerminal?: (status: number) => void,
): () => void {
  let closed = false;
  let controller: AbortController | null = null;
  let timer = 0;
  let failures = 0;
  const visible = () => document.visibilityState !== "hidden";

  const connect = async () => {
    if (closed || !visible() || controller) return;
    const active = new AbortController();
    controller = active;
    try {
      const response = await fetchEventStream(path, {
        signal: active.signal,
        headers: { Accept: "text/event-stream" },
      });
      if (TERMINAL_STREAM_STATUSES.has(response.status)) {
        // Stop for good; the subscriber decides what the reviewer is told.
        closed = true;
        onTerminal?.(response.status);
        return;
      }
      if (!response.ok || !response.body) throw new Error(`Review stream: ${response.status}`);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let data: string[] = [];
      try {
        while (!active.signal.aborted) {
          const chunk = await reader.read();
          if (chunk.done) break;
          buffer += decoder.decode(chunk.value, { stream: true });
          let newline: number;
          while ((newline = buffer.indexOf("\n")) >= 0) {
            const line = buffer.slice(0, newline).replace(/\r$/, "");
            buffer = buffer.slice(newline + 1);
            if (!line) {
              if (data.length) {
                const state = JSON.parse(data.join("\n")) as T;
                data = [];
                if (active.signal.aborted) return;
                failures = 0;
                await onState(state);
              }
            } else if (line.startsWith("data:")) {
              data.push(line.slice(5).replace(/^ /, ""));
            }
          }
        }
      } finally {
        await reader.cancel().catch(() => {});
        reader.releaseLock();
      }
    } catch {
      // A reconnect always starts with a fresh authoritative projection.
    } finally {
      if (controller === active) controller = null;
      if (!closed && !active.signal.aborted && visible()) {
        timer = window.setTimeout(
          () => void connect(),
          Math.min(30_000, retryMs * 2 ** Math.min(failures++, 5)),
        );
      }
    }
  };

  const visibility = () => {
    window.clearTimeout(timer);
    if (document.visibilityState === "hidden") {
      controller?.abort();
      controller = null;
    } else {
      failures = 0;
      void connect();
    }
  };

  document.addEventListener("visibilitychange", visibility);
  void connect();
  return () => {
    closed = true;
    window.clearTimeout(timer);
    controller?.abort();
    document.removeEventListener("visibilitychange", visibility);
  };
}
