import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { subscribeReviewState } from "./reviewStateEvents";

const auth = vi.fn();
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };

describe("Review SSE transport", () => {
  let visibility = "visible";
  let stop: (() => void) | undefined;

  beforeEach(() => {
    vi.useFakeTimers();
    auth.mockReset();
    visibility = "visible";
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => visibility });
  });

  afterEach(() => {
    stop?.();
    stop = undefined;
    vi.useRealTimers();
  });

  function connection() {
    let stream!: ReadableStreamDefaultController<Uint8Array>;
    auth.mockImplementationOnce(async (_path, init) => {
      const body = new ReadableStream<Uint8Array>({
        start(controller) {
          stream = controller;
          init.signal.addEventListener("abort", () => controller.error(new DOMException("Aborted", "AbortError")));
        },
      });
      return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
    });
    return (text: string) => stream.enqueue(new TextEncoder().encode(text));
  }

  it("uses authenticated fetch and decodes split frames, CRLF, comments and multiple events", async () => {
    const send = connection();
    const receive = vi.fn();
    stop = subscribeReviewState("/api/reviews/r/live-state/events", receive, auth);
    await flush();
    send(': keepalive\r\nevent: live-state\r\ndata: {"text":\r\n');
    send('data: "hello"}\r\n\r\ndata: {"text":"second"}\n\n');
    await flush();
    expect(receive.mock.calls).toEqual([[{ text: "hello" }], [{ text: "second" }]]);
    expect(auth).toHaveBeenCalledTimes(1);
    expect(auth.mock.calls[0][1].headers.Accept).toBe("text/event-stream");
    await vi.advanceTimersByTimeAsync(60_000);
    expect(auth).toHaveBeenCalledTimes(1);
  });

  it("aborts on hiding, starts fresh on showing, and removes listeners on cleanup", async () => {
    const send = connection();
    stop = subscribeReviewState("/events", vi.fn(), auth);
    await flush();
    const signal = auth.mock.calls[0][1].signal as AbortSignal;
    visibility = "hidden";
    document.dispatchEvent(new Event("visibilitychange"));
    await flush();
    expect(signal.aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(auth).toHaveBeenCalledTimes(1);
    connection();
    visibility = "visible";
    document.dispatchEvent(new Event("visibilitychange"));
    await flush();
    expect(auth).toHaveBeenCalledTimes(2);
    stop();
    expect(auth.mock.calls[1][1].signal.aborted).toBe(true);
    document.dispatchEvent(new Event("visibilitychange"));
    expect(auth).toHaveBeenCalledTimes(2);
    expect(send).toBeTypeOf("function");
  });

  it("reconnects with backoff after failures and retries failed projections", async () => {
    auth.mockRejectedValueOnce(new Error("offline"));
    const send = connection();
    const receive = vi.fn().mockRejectedValueOnce(new Error("projection unavailable"));
    stop = subscribeReviewState("/events", receive, auth, 100);
    await flush();
    expect(auth).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(100);
    expect(auth).toHaveBeenCalledTimes(2);
    send('data: {"cursor":"new"}\n\n');
    await flush();
    expect(receive).toHaveBeenCalledWith({ cursor: "new" });
    connection();
    await vi.advanceTimersByTimeAsync(100);
    expect(auth).toHaveBeenCalledTimes(3);
    stop();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(auth).toHaveBeenCalledTimes(3);
  });

  it.each([401, 403, 404, 410])("stops reconnecting on a terminal %i and reports it", async (status) => {
    auth.mockResolvedValue(new Response("refused", { status }));
    const terminal = vi.fn();
    stop = subscribeReviewState("/events", vi.fn(), auth, 100, terminal);
    await flush();
    expect(terminal).toHaveBeenCalledWith(status);
    await vi.advanceTimersByTimeAsync(60_000);
    visibility = "hidden";
    document.dispatchEvent(new Event("visibilitychange"));
    visibility = "visible";
    document.dispatchEvent(new Event("visibilitychange"));
    await flush();
    expect(auth).toHaveBeenCalledTimes(1);
    expect(terminal).toHaveBeenCalledTimes(1);
  });

  it("keeps retrying a transient server failure without reporting it as terminal", async () => {
    auth.mockResolvedValue(new Response("busy", { status: 503 }));
    const terminal = vi.fn();
    stop = subscribeReviewState("/events", vi.fn(), auth, 100, terminal);
    await flush();
    await vi.advanceTimersByTimeAsync(100);
    expect(auth).toHaveBeenCalledTimes(2);
    expect(terminal).not.toHaveBeenCalled();
  });
});
