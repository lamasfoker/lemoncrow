/** OpenCode 2 CLI sidebar, loaded by cli.json plugins. */
import { Plugin } from "@opencode/plugin/tui";
import { createMemo, createSignal, For, onCleanup, Show } from "solid-js";
import { existsSync, readFileSync, statSync } from "node:fs";
const STATUS_FILE_ENV = "LEMONCROW_STATUS_FILE";
const POLL_MS = 1000;
const TOP_TOOLS = 4;

type Snapshot = {
  provider?: string;
  model?: string;
  input_tokens?: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
  output_tokens?: number;
  context_tokens?: number;
  cost_usd?: number;
  saved_usd?: number;
  cache_efficiency_pct?: number;
  turns?: number;
  tool_calls?: Record<string, number>;
  tool_call_total?: number;
  mcp_calls?: number;
  index?: {
    present?: boolean;
    files?: number;
    symbols?: number;
    languages?: number;
    indexed_at?: number;
    zoekt?: boolean;
  };
};

const money = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 4,
});

function tokens(value: number | undefined) {
  const n = value ?? 0;
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return Math.round(n / 1_000) + "k";
  return String(n);
}

function ago(seconds: number | undefined) {
  const value = seconds ?? 0;
  if (value <= 0) return "";
  const delta = Math.max(0, Date.now() / 1000 - value);
  if (delta < 90) return "just now";
  if (delta < 3600) return Math.round(delta / 60) + "m ago";
  if (delta < 86_400) return Math.round(delta / 3600) + "h ago";
  return Math.round(delta / 86_400) + "d ago";
}

function useSnapshot() {
  const path = process.env[STATUS_FILE_ENV];
  const [snapshot, setSnapshot] = createSignal<Snapshot | undefined>();
  // With a snapshot path configured the panel appears immediately, zeroed,
  // instead of only materialising after the first turn -- an empty sidebar
  // reads as "the plugin is not installed".
  setSnapshot({});
  if (!path) return snapshot;

  let seen = 0;
  const load = () => {
    try {
      if (!existsSync(path)) return;
      // Re-parse only when the gateway actually rewrote the file.
      const mtime = statSync(path).mtimeMs;
      if (mtime === seen) return;
      seen = mtime;
      setSnapshot(JSON.parse(readFileSync(path, "utf8")) as Snapshot);
    } catch {
      // A torn read between write and rename resolves on the next tick.
    }
  };

  load();
  const timer = setInterval(load, POLL_MS);
  onCleanup(() => clearInterval(timer));
  return snapshot;
}

function View(props: { context: any }) {
  const theme = () => props.context.theme;
  const snapshot = useSnapshot();
  const has = createMemo(() => snapshot() !== undefined);

  const model = createMemo(() => {
    const value = snapshot()?.model ?? "";
    // Strip the litellm provider prefix; the sidebar is narrow.
    return value.includes("/")
      ? value.slice(value.lastIndexOf("/") + 1)
      : value;
  });

  const usage = createMemo(() => {
    const item = snapshot();
    return `I ${tokens(item?.input_tokens)} · C ${tokens(item?.cache_read_tokens)} · O ${tokens(item?.output_tokens)}`;
  });

  const index = createMemo(() => snapshot()?.index);

  const toolList = createMemo(() =>
    Object.entries(snapshot()?.tool_calls ?? {})
      .sort((a, b) => b[1] - a[1])
      .slice(0, TOP_TOOLS),
  );

  return (
    <Show when={has()}>
      <box>
        <text fg={theme().text.base}>
          <b>LemonCrow</b>
        </text>
        <Show when={model()}>
          <text fg={theme().text.muted}>{model()}</text>
        </Show>
        <Show when={process.env[STATUS_FILE_ENV]}
          fallback={<text fg={theme().text.muted}>Metrics appear when a snapshot is available</text>}>
          <text fg={theme().text.muted}>{usage()}</text>
          <text fg={theme().text.muted}>{tokens(snapshot()?.context_tokens)} ctx</text>
        </Show>
        <Show when={(snapshot()?.cache_efficiency_pct ?? 0) > 0}>
          <text fg={theme().text.muted}>
            {Math.round(snapshot()?.cache_efficiency_pct ?? 0)}% cached
          </text>
        </Show>
        <Show when={process.env[STATUS_FILE_ENV]}>
          <text fg={theme().text.muted}>
            ~{money.format(snapshot()?.cost_usd ?? 0)} priced
          </text>
        </Show>
        <Show when={(snapshot()?.saved_usd ?? 0) > 0}>
          <text fg={theme().text.base}>
            est. {money.format(snapshot()?.saved_usd ?? 0)} saved
          </text>
        </Show>
        <Show when={(snapshot()?.tool_call_total ?? 0) > 0}>
          <text fg={theme().text.muted}>
            {snapshot()?.tool_call_total} tool calls
            <Show when={(snapshot()?.mcp_calls ?? 0) > 0}>
              <span> ({snapshot()?.mcp_calls} mcp)</span>
            </Show>
          </text>
          <For each={toolList()}>
            {([name, count]) => (
              <text fg={theme().text.muted}>
                {"  "}
                {name} {count}
              </text>
            )}
          </For>
        </Show>
        <Show when={index()?.present}>
          <box marginTop={1}>
            <text fg={theme().text.base}>
              <b>Local index</b>
            </text>
            <text fg={theme().text.muted}>
              {tokens(index()?.files)} files · {tokens(index()?.symbols)}{" "}
              symbols
            </text>
            <Show when={(index()?.languages ?? 0) > 0}>
              <text fg={theme().text.muted}>{index()?.languages} languages</text>
            </Show>
            <Show when={ago(index()?.indexed_at)}>
              <text fg={theme().text.muted}>
                indexed {ago(index()?.indexed_at)}
              </text>
            </Show>
            <text fg={index()?.zoekt ? theme().text.base : theme().text.muted}>
              {index()?.zoekt ? "zoekt ready" : "zoekt off"}
            </text>
          </box>
        </Show>
      </box>
    </Show>
  );
}


export default Plugin.define({
  id: "lemoncrow.status",
  setup(context) {
    return context.ui.slot({
      append: "sidebar.content",
      render: () => <View context={context} />,
    });
  },
});
