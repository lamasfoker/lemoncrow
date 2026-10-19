import { apiUrl } from "../api";
import { authenticatedFetch, type ReviewAskStreamEvent } from "../review/reviewApi";
import type { AskTransport } from "../review/ReviewAskDrawer";
import type {
  ReviewAskConversation,
  ReviewAskConversationOrigin,
  ReviewAskConversationSummary,
  ReviewAskDriver,
  ReviewAskInput,
  ReviewAskMessage,
  ReviewAskReference,
  ReviewAskResult,
  ReviewEvidence,
} from "../review/types";

const ALLOWED_DRIVERS: ReviewAskDriver[] = ["configured", "claude", "codex", "ollama", "openai", "litellm"];
const CODE_HISTORY_VERSION = 2;
const LEGACY_CONVERSATION_ID = "legacy";
const MAX_CONVERSATIONS = 30;
const MAX_MESSAGES_PER_CONVERSATION = 80;

interface SavedCodeConversation extends ReviewAskConversationSummary {
  messages: ReviewAskMessage[];
}

interface SavedCodeState {
  version: 2;
  active_conversation_id: string;
  conversations: SavedCodeConversation[];
}

function readText(file: File): Promise<string> {
  if (typeof file.text === "function") return file.text();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(reader.error ?? new Error("Could not read attachment"));
    reader.readAsText(file);
  });
}

type CodeAnswer = { body: string; references: ReviewAskReference[] };
type CodeEvent = Exclude<ReviewAskStreamEvent, { type: "result" }> | { type: "result"; result: CodeAnswer };

function isMessage(item: unknown): item is ReviewAskMessage {
  return Boolean(
    item && typeof item === "object" && "role" in item && "body" in item
    && "references" in item && Array.isArray((item as ReviewAskMessage).references)
    && "action" in item && (item as ReviewAskMessage).action && typeof (item as ReviewAskMessage).action === "object",
  );
}

function titleFromMessages(messages: ReviewAskMessage[]): string {
  const first = messages.find((message) => message.role === "human");
  return first ? first.body.replace(/\s+/g, " ").trim().slice(0, 160) || "Previous conversation" : "Previous conversation";
}

function originFromMessages(messages: ReviewAskMessage[]): ReviewAskConversationOrigin {
  const first = messages.find((message) => message.role === "human") ?? messages[0];
  return {
    kind: "code",
    path: first?.scope.path || undefined,
    symbol: first?.scope.symbol || undefined,
    start_line: first?.scope.start_line || undefined,
    end_line: first?.scope.end_line || undefined,
  };
}

function validConversation(item: unknown): item is SavedCodeConversation {
  if (!item || typeof item !== "object") return false;
  const row = item as Partial<SavedCodeConversation>;
  return typeof row.id === "string"
    && typeof row.title === "string"
    && Boolean(row.origin && typeof row.origin === "object")
    && typeof row.created_at === "string"
    && typeof row.updated_at === "string"
    && Array.isArray(row.messages)
    && row.messages.every(isMessage);
}

function emptyState(): SavedCodeState {
  return { version: CODE_HISTORY_VERSION, active_conversation_id: "", conversations: [] };
}

function savedState(key: string): SavedCodeState {
  try {
    const saved = JSON.parse(window.localStorage.getItem(key) || "null") as unknown;
    if (Array.isArray(saved)) {
      const messages = saved.filter(isMessage);
      if (messages.length === 0) return emptyState();
      const createdAt = messages[0]?.created_at || new Date(0).toISOString();
      const updatedAt = messages.at(-1)?.created_at || createdAt;
      const conversation: SavedCodeConversation = {
        id: LEGACY_CONVERSATION_ID,
        title: titleFromMessages(messages),
        origin: originFromMessages(messages),
        created_at: createdAt,
        updated_at: updatedAt,
        message_count: messages.length,
        legacy: true,
        messages: messages.map((message) => ({ ...message, conversation_id: LEGACY_CONVERSATION_ID })),
      };
      return { version: CODE_HISTORY_VERSION, active_conversation_id: conversation.id, conversations: [conversation] };
    }
    if (!saved || typeof saved !== "object") return emptyState();
    const state = saved as Partial<SavedCodeState>;
    if (state.version !== CODE_HISTORY_VERSION || !Array.isArray(state.conversations)) return emptyState();
    const conversations = state.conversations.filter(validConversation).map((conversation) => ({
      ...conversation,
      message_count: conversation.messages.length,
      messages: conversation.messages.slice(-MAX_MESSAGES_PER_CONVERSATION),
    }));
    const active = typeof state.active_conversation_id === "string"
      && conversations.some((conversation) => conversation.id === state.active_conversation_id)
      ? state.active_conversation_id
      : conversations[0]?.id ?? "";
    return { version: CODE_HISTORY_VERSION, active_conversation_id: active, conversations };
  } catch {
    return emptyState();
  }
}

function persistState(key: string, state: SavedCodeState): void {
  const conversations = [...state.conversations]
    .sort((left, right) => right.updated_at.localeCompare(left.updated_at))
    .slice(0, MAX_CONVERSATIONS)
    .map((conversation) => ({
      ...conversation,
      message_count: conversation.messages.length,
      messages: conversation.messages.slice(-MAX_MESSAGES_PER_CONVERSATION),
    }));
  const active = conversations.some((conversation) => conversation.id === state.active_conversation_id)
    ? state.active_conversation_id
    : conversations[0]?.id ?? "";
  try {
    window.localStorage.setItem(key, JSON.stringify({ version: CODE_HISTORY_VERSION, active_conversation_id: active, conversations }));
  } catch {
    // The active answer remains visible even when browser storage is unavailable/full.
  }
}

function summary(conversation: SavedCodeConversation): ReviewAskConversationSummary {
  const { messages: _messages, ...rest } = conversation;
  return { ...rest, message_count: conversation.messages.length };
}

function newConversationId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return `code-${crypto.randomUUID()}`;
  return `code-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function createConversation(origin: ReviewAskConversationOrigin): SavedCodeConversation {
  const now = new Date().toISOString();
  return {
    id: newConversationId(),
    title: "New conversation",
    origin: { ...origin, kind: "code" },
    created_at: now,
    updated_at: now,
    message_count: 0,
    messages: [],
  };
}

async function readCodeStream(response: Response, onEvent: (event: ReviewAskStreamEvent) => void): Promise<CodeAnswer> {
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string };
    throw new Error(payload.detail || `Code Ask failed (${response.status})`);
  }
  if (!response.body) throw new Error("Code Ask returned no stream");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result: CodeAnswer | null = null;
  const consume = (line: string) => {
    if (!line.trim()) return;
    const event = JSON.parse(line) as CodeEvent;
    if (event.type === "error") throw new Error(event.detail);
    if (event.type === "result") result = event.result;
    else onEvent(event);
  };
  for (;;) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    lines.forEach(consume);
    if (done) break;
  }
  consume(buffer);
  if (!result) throw new Error("Code Ask ended before an answer was ready");
  return result;
}

export function createCodeAskTransport(projectRoot: string): AskTransport {
  const key = `lemoncrow.code.ask:${projectRoot}`;
  const attachments = new Map<string, { name: string; content: string }>();

  const loadOptions = async () => {
    const response = await authenticatedFetch(apiUrl("/v1/code-map/ask/options"));
    if (!response.ok) throw new Error(`Could not load Code Ask (${response.status})`);
    const options = await response.json() as { driver?: string; model?: string };
    const driver = ALLOWED_DRIVERS.includes(options.driver as ReviewAskDriver) ? options.driver as ReviewAskDriver : "configured";
    return { driver, model: options.model ?? "" };
  };

  return {
    allowedDrivers: ALLOWED_DRIVERS,
    async upload(file: File): Promise<ReviewEvidence> {
      const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      attachments.set(id, { name: file.name, content: await readText(file) });
      return {
        id, review_id: projectRoot, revision_id: "", revision_number: null,
        kind: "document", title: file.name, path: "", url: "", content_hash: "",
        mime_type: file.type, bytes: file.size, source: "external", source_ref: "",
        verification_status: "", detail: "", created_at: new Date().toISOString(),
        status: "current", content_url: "",
      };
    },
    async load(conversationId?: string): Promise<ReviewAskConversation> {
      const options = await loadOptions();
      const state = savedState(key);
      const selectedId = conversationId || state.active_conversation_id || state.conversations[0]?.id || "";
      if (conversationId && !state.conversations.some((conversation) => conversation.id === conversationId)) {
        throw new Error("This Code Ask conversation is no longer available.");
      }
      const selected = state.conversations.find((conversation) => conversation.id === selectedId);
      if (selectedId !== state.active_conversation_id) persistState(key, { ...state, active_conversation_id: selectedId });
      return {
        review_id: projectRoot,
        latest_revision_number: 0,
        messages: selected?.messages ?? [],
        conversations: [...state.conversations]
          .sort((left, right) => right.updated_at.localeCompare(left.updated_at))
          .map(summary),
        active_conversation_id: selectedId,
        ai: {
          driver: options.driver,
          model: options.model,
          models: options.model ? [options.model] : [],
          models_by_driver: {},
        },
      };
    },
    async startConversation(origin: ReviewAskConversationOrigin): Promise<ReviewAskConversationSummary> {
      const state = savedState(key);
      const conversation = createConversation(origin);
      persistState(key, {
        version: CODE_HISTORY_VERSION,
        active_conversation_id: conversation.id,
        conversations: [conversation, ...state.conversations],
      });
      return summary(conversation);
    },
    async stream(input: ReviewAskInput, onEvent: (event: ReviewAskStreamEvent) => void, signal?: AbortSignal): Promise<ReviewAskResult> {
      if (!input.path) throw new Error("Select a file before asking about code.");
      const state = savedState(key);
      let conversationId = input.conversation_id || state.active_conversation_id;
      let conversation = state.conversations.find((item) => item.id === conversationId);
      if (conversationId && !conversation) throw new Error("This Code Ask conversation is no longer available.");
      if (!conversation) {
        conversation = createConversation({
          kind: "code",
          path: input.path,
          symbol: input.symbol,
          start_line: input.start_line,
          end_line: input.end_line,
        });
        conversationId = conversation.id;
      }
      const previous = conversation.messages;
      const response = await authenticatedFetch(apiUrl("/v1/code-map/ask/stream"), {
        method: "POST",
        ...(signal ? { signal } : {}),
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project_root: projectRoot,
          path: input.path,
          line: input.start_line || undefined,
          end_line: input.end_line || undefined,
          symbol: input.symbol || "",
          selected_text: input.selected_text || "",
          question: input.body,
          mentioned_paths: input.mentioned_paths ?? [],
          attachments: (input.evidence_ids ?? []).flatMap((id) => {
            const file = attachments.get(id);
            return file ? [{ name: file.name, content: file.content }] : [];
          }),
          history: previous.slice(-12).map((message) => ({ role: message.role === "human" ? "user" : "assistant", body: message.body })),
          driver: ALLOWED_DRIVERS.includes(input.driver as ReviewAskDriver) ? input.driver : "configured",
          model: input.model || "",
        }),
      });
      const answer = await readCodeStream(response, onEvent);
      const scope = {
        target_unit_key: "", path: input.path, symbol: input.symbol || "", side: "current",
        start_line: input.start_line || 0, end_line: input.end_line || input.start_line || 0,
      };
      const timestamp = new Date().toISOString();
      const base = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      const human: ReviewAskMessage = {
        id: `${base}-human`, review_id: projectRoot, revision_id: "", revision_number: null,
        role: "human", intent: "question", body: input.body, scope,
        references: [
          { path: input.path, start_line: input.start_line, end_line: input.end_line },
          ...(input.evidence_ids ?? []).flatMap((id) => {
            const file = attachments.get(id);
            return file ? [{ evidence_id: id, label: file.name }] : [];
          }),
        ],
        action: {}, created_at: timestamp, conversation_id: conversationId,
      };
      const assistant: ReviewAskMessage = {
        id: `${base}-assistant`, review_id: projectRoot, revision_id: "", revision_number: null,
        role: "assistant", intent: "question", body: answer.body, scope,
        references: answer.references ?? [], action: {}, created_at: timestamp, conversation_id: conversationId,
      };
      for (const id of input.evidence_ids ?? []) attachments.delete(id);
      const updated: SavedCodeConversation = {
        ...conversation,
        title: conversation.title === "New conversation" ? titleFromMessages([human]) : conversation.title,
        updated_at: timestamp,
        messages: [...previous, human, assistant].slice(-MAX_MESSAGES_PER_CONVERSATION),
        message_count: Math.min(MAX_MESSAGES_PER_CONVERSATION, previous.length + 2),
      };
      persistState(key, {
        version: CODE_HISTORY_VERSION,
        active_conversation_id: conversationId,
        conversations: [updated, ...state.conversations.filter((item) => item.id !== conversationId)],
      });
      return { review_id: projectRoot, latest_revision_number: 0, conversation_id: conversationId, human, assistant };
    },
  };
}
