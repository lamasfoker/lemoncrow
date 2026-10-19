import { beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "../review/reviewApi";
import type { ReviewAskMessage } from "../review/types";
import { createCodeAskTransport } from "./codeAskTransport";

vi.mock("../review/reviewApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../review/reviewApi")>();
  return { ...actual, authenticatedFetch: vi.fn() };
});

function message(id: string, role: "human" | "assistant", body: string): ReviewAskMessage {
  return {
    id,
    review_id: "/repo",
    revision_id: "",
    revision_number: null,
    role,
    intent: "question",
    body,
    scope: { target_unit_key: "", path: "src/app.ts", symbol: "", side: "current", start_line: 1, end_line: 1 },
    references: [],
    action: {},
    created_at: "2026-10-05T16:00:00.000Z",
  };
}

function streamResponse(answer: string): Response {
  const encoder = new TextEncoder();
  const lines = [`${JSON.stringify({ type: "result", result: { body: answer, references: [] } })}\n`];
  let index = 0;
  return {
    ok: true,
    body: {
      getReader: () => ({
        read: async () => index < lines.length
          ? { value: encoder.encode(lines[index++]), done: false }
          : { value: undefined, done: true },
      }),
    },
  } as unknown as Response;
}

describe("createCodeAskTransport conversation history", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.clearAllMocks();
    vi.mocked(authenticatedFetch).mockImplementation(async (url) => {
      if (String(url).includes("/ask/options")) {
        return { ok: true, json: async () => ({ driver: "configured", model: "" }) } as Response;
      }
      return streamResponse("answer");
    });
  });

  it("lazily upgrades the old flat transcript into one legacy conversation", async () => {
    window.localStorage.setItem("lemoncrow.code.ask:/repo", JSON.stringify([
      message("h1", "human", "Why is this helper needed?"),
      message("a1", "assistant", "It centralizes the boundary."),
    ]));
    const transport = createCodeAskTransport("/repo");

    const loaded = await transport.load();

    expect(loaded.active_conversation_id).toBe("legacy");
    expect(loaded.conversations).toEqual([
      expect.objectContaining({ id: "legacy", title: "Why is this helper needed?", message_count: 2, legacy: true }),
    ]);
    expect(loaded.messages.map((item) => item.body)).toEqual([
      "Why is this helper needed?",
      "It centralizes the boundary.",
    ]);
  });

  it("starts a clean thread whose model history cannot see another Code conversation", async () => {
    const transport = createCodeAskTransport("/repo");
    const first = await transport.startConversation?.({ kind: "code", path: "src/first.ts" });
    expect(first).toBeTruthy();
    await transport.stream({
      revision_number: 0,
      conversation_id: first!.id,
      body: "Explain the first file",
      path: "src/first.ts",
    }, vi.fn());

    const second = await transport.startConversation?.({ kind: "code", path: "src/second.ts" });
    expect(second).toBeTruthy();
    await transport.stream({
      revision_number: 0,
      conversation_id: second!.id,
      body: "Explain the second file",
      path: "src/second.ts",
    }, vi.fn());

    const streamCalls = vi.mocked(authenticatedFetch).mock.calls.filter(([url]) => String(url).includes("/ask/stream"));
    expect(streamCalls).toHaveLength(2);
    const firstBody = JSON.parse(String(streamCalls[0][1]?.body));
    const secondBody = JSON.parse(String(streamCalls[1][1]?.body));
    expect(firstBody.history).toEqual([]);
    expect(secondBody.history).toEqual([]);

    const firstLoaded = await transport.load(first!.id);
    const secondLoaded = await transport.load(second!.id);
    expect(firstLoaded.messages.map((item) => item.body)).toEqual(["Explain the first file", "answer"]);
    expect(secondLoaded.messages.map((item) => item.body)).toEqual(["Explain the second file", "answer"]);
    expect(secondLoaded.conversations?.map((item) => item.id)).toEqual([second!.id, first!.id]);
  });
});
