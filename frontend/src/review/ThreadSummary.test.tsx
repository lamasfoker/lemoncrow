/**
 * DS0: the thread summary is opened on request, reads without a model call,
 * shows the thread's state from the comments (never from the summary), links
 * every statement to its messages and says when it is out of date.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import AnnotationCard from "./AnnotationCard";
import { resetThreadSummaryAvailability } from "./reviewApi";
import type { Annotation, ThreadSummaryView } from "./types";

function annotation(overrides: Partial<Annotation> = {}): Annotation {
  return {
    id: "ann-1",
    review_id: "rev-1",
    revision_id: "rrv-1",
    parent_id: "",
    kind: "comment",
    state: "open",
    body: "Why does an expired record return invalid now?",
    created_by: "local",
    created_by_actor: "human",
    source: "human",
    source_id: "local",
    created_at: "",
    updated_at: "",
    anchor_method: "identical_blob",
    anchor_method_label: "file unchanged since the comment",
    anchor_exact: true,
    anchor_detail: "file unchanged in this revision",
    anchored: true,
    path: "src/session.py",
    side: "new",
    start_line: 4,
    end_line: 4,
    unit_key: "sym:abc",
    symbol: "refresh",
    origin_symbol: "",
    ...overrides,
  };
}

const ROOT = annotation();
const REPLIES = [
  annotation({ id: "ann-2", parent_id: "ann-1", body: "The store forgets users after a day." }),
  annotation({ id: "ann-3", parent_id: "ann-1", body: "Please confirm the one-day expiry." }),
];

function view(overrides: Partial<ThreadSummaryView> = {}, summary: Partial<NonNullable<ThreadSummaryView["summary"]>> | null = {}): ThreadSummaryView {
  return {
    thread_id: "ann-1",
    eligible: true,
    thread: {
      state: "resolved",
      resolved_revision_id: "rrv-1",
      turn_owner_kind: "none",
      author_response: "none",
      message_count: 3,
      last_message_at: "",
    },
    summary: summary === null
      ? null
      : {
          topic: { text: "Why expired records now read as invalid.", message_refs: ["annotation:ann-1"] },
          open_asks: [{ text: "Confirm the one-day expiry.", message_refs: ["annotation:ann-3"] }],
          points: [{ text: "The store forgets users after a day.", message_refs: ["annotation:ann-2", "annotation:ann-3"] }],
          message_refs: ["annotation:ann-1", "annotation:ann-3", "annotation:ann-2"],
          current: true,
          driver: "openai",
          model: "gpt-test",
          created_at: "",
          run_id: "run-1",
          ...summary,
        },
    last_attempt: null,
    ...overrides,
  };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

type Route = (url: string, method: string) => Response | undefined;

function stubFetch(route: Route) {
  const spy = vi.fn(async (url: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const answer = route(url, method);
    if (!answer) throw new Error(`unexpected ${method} ${url}`);
    return answer;
  });
  vi.stubGlobal("fetch", spy);
  return spy;
}

function calls(spy: ReturnType<typeof stubFetch>): string[] {
  return (spy.mock.calls as unknown as [string, RequestInit | undefined][]).map(
    ([url, init]) => `${(init?.method ?? "GET").toUpperCase()} ${url}`,
  );
}

function mount(all: Annotation[]) {
  return render(
    <AnnotationCard
      comments={[ROOT]}
      all={all}
      label="Comment"
      composing={false}
      busy={false}
      onSubmit={vi.fn()}
      onCancel={vi.fn()}
      onResolve={vi.fn()}
      onReopen={vi.fn()}
    />,
  );
}

const LIST = "/api/reviews/rev-1/thread-summaries";
const ONE = "/api/reviews/rev-1/threads/ann-1/summary";

afterEach(() => {
  vi.unstubAllGlobals();
  resetThreadSummaryAvailability();
});

describe("ThreadSummary", () => {
  it("is absent where the deployment offers no summaries, and for a comment with one reply", async () => {
    const spy = stubFetch((url) => (url === LIST ? json({ available: false, summaries: [] }) : undefined));
    const { container, unmount } = mount([ROOT, ...REPLIES]);
    await waitFor(() => expect(calls(spy)).toEqual([`GET ${LIST}`]));
    expect(container.querySelector("[data-testid='thread-summary']")).toBeNull();
    unmount();
    resetThreadSummaryAvailability();
    spy.mockClear();
    mount([ROOT, REPLIES[0]]);
    expect(screen.queryByTestId("thread-summary")).toBeNull();
    expect(spy).not.toHaveBeenCalled();
  });

  it("opening reads only, and shows the thread state from the comments beside a linked summary", async () => {
    const spy = stubFetch((url, method) => {
      if (url === LIST) return json({ available: true, min_messages: 3, summaries: [] });
      if (url === ONE && method === "GET") return json({ available: true, summary: view() });
      return undefined;
    });
    mount([ROOT, ...REPLIES]);
    const disclosure = await screen.findByText("Thread summary · AI");
    expect(calls(spy)).toEqual([`GET ${LIST}`]);
    await userEvent.click(disclosure);
    expect(await screen.findByTestId("thread-summary-topic")).toHaveTextContent("Why expired records now read as invalid.#1");
    expect(screen.getByTestId("thread-summary-state")).toHaveTextContent("Thread resolved · 3 messages");
    expect(screen.getAllByTestId("thread-summary-ref").map((item) => item.textContent)).toEqual(["#1", "#3", "#2", "#3"]);
    expect(screen.getByText("Open asks in the conversation")).toBeInTheDocument();
    expect(screen.queryByTestId("thread-summary-stale")).toBeNull();
    expect(screen.queryByRole("button", { name: /summar/i })).toBeNull();
    expect(calls(spy)).toEqual([`GET ${LIST}`, `GET ${ONE}`]);
  });

  it("marks a stale summary and refreshes only when asked", async () => {
    const spy = stubFetch((url, method) => {
      if (url === LIST) return json({ available: true, min_messages: 3, summaries: [] });
      if (url === ONE && method === "GET") return json({ available: true, summary: view({}, { current: false }) });
      if (url === ONE && method === "POST") {
        return json({ result: { status: "ready", reason: "", provider_calls: 1, cache_hit: false }, summary: view() });
      }
      return undefined;
    });
    mount([ROOT, ...REPLIES]);
    await userEvent.click(await screen.findByText("Thread summary · AI"));
    expect(await screen.findByTestId("thread-summary-stale")).toHaveTextContent("Out of date");
    expect(calls(spy).filter((item) => item.startsWith("POST"))).toEqual([]);
    await userEvent.click(screen.getByRole("button", { name: "Refresh summary" }));
    await waitFor(() => expect(screen.queryByTestId("thread-summary-stale")).toBeNull());
    expect(calls(spy).filter((item) => item.startsWith("POST"))).toEqual([`POST ${ONE}`]);
  });

  it("offers Summarize for an unsummarized thread and shows why a run made nothing", async () => {
    stubFetch((url, method) => {
      if (url === LIST) return json({ available: true, min_messages: 3, summaries: [] });
      if (url === ONE && method === "GET") return json({ available: true, summary: view({}, null) });
      if (url === ONE && method === "POST") {
        return json({
          result: { status: "unavailable", reason: "AI content access refused: src/session.py", provider_calls: 0, cache_hit: false },
          summary: view({}, null),
        });
      }
      return undefined;
    });
    mount([ROOT, ...REPLIES]);
    await userEvent.click(await screen.findByText("Thread summary · AI"));
    await userEvent.click(await screen.findByRole("button", { name: "Summarize thread" }));
    expect(await screen.findByTestId("thread-summary-error")).toHaveTextContent("AI content access refused");
    expect(screen.queryByTestId("thread-summary-topic")).toBeNull();
  });
});
