import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import BriefArchitectureDelta from "./BriefArchitectureDelta";
import type { ReviewArchitectureDelta } from "./types";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function materialDelta(): ReviewArchitectureDelta {
  return {
    version: 1, digest: "old-snapshot", status: "ready", reason: "", base_identity: "base", snapshot_identity: "old",
    grouping: { strategy: "directory", version: 1, stable_for_identical_input: true, components: [] },
    summary: {
      material: true, status: "changed", changed_components: 1, changed_nodes: 1, changed_edges: 1,
      added_edges: 1, removed_edges: 0, modified_edges: 0, context_only_edges: 0, unknown_edges: 0,
      important_concerns: [], exactness: "snapshot", degraded: [],
    },
    flow: { applicable: false, kind: "", reason: "", steps: [] },
    graph: { level: "architecture", focus: "", nodes: [], edges: [], truncated: false },
  };
}

describe("lazy architecture Snapshot identity", () => {
  it.each([false, true])("ignores a response after Snapshot advancement (eager=%s)", async (eager) => {
    const old = deferred<ReviewArchitectureDelta | null>();
    const loadOld = vi.fn(() => old.promise);
    const loadNew = vi.fn().mockResolvedValue(null);
    const { rerender } = render(<BriefArchitectureDelta load={loadOld} onOpen={vi.fn()} eager={eager} />);
    if (!eager) await userEvent.click(screen.getByRole("button", { name: "Check architecture changes" }));
    await act(async () => {});
    expect(loadOld).toHaveBeenCalledTimes(1);
    rerender(<BriefArchitectureDelta load={loadNew} onOpen={vi.fn()} eager={eager} />);
    await act(async () => old.resolve(materialDelta()));
    expect(screen.queryByText("Architecture moved")).toBeNull();
    if (!eager) {
      expect(loadNew).not.toHaveBeenCalled();
      await userEvent.click(screen.getByRole("button", { name: "Check architecture changes" }));
    }
    expect(loadNew).toHaveBeenCalledTimes(1);
  });

  it("keeps failed manual loads retryable without claiming no movement", async () => {
    const load = vi.fn().mockRejectedValueOnce(new Error("unavailable")).mockResolvedValueOnce(materialDelta());
    render(<BriefArchitectureDelta load={load} onOpen={vi.fn()} eager={false} />);
    await userEvent.click(screen.getByRole("button", { name: "Check architecture changes" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Architecture details could not load. Try again.");
    expect(screen.queryByText("No material architecture changes")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Check architecture changes" }));
    expect(await screen.findByText("Architecture moved")).toBeTruthy();
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("cancels a manually requested response on unmount", async () => {
    const reply = deferred<ReviewArchitectureDelta | null>();
    const load = vi.fn(() => reply.promise);
    const { unmount } = render(<BriefArchitectureDelta load={load} onOpen={vi.fn()} eager={false} />);
    await userEvent.click(screen.getByRole("button", { name: "Check architecture changes" }));
    unmount();
    await act(async () => reply.resolve(materialDelta()));
    expect(screen.queryByText("Architecture moved")).toBeNull();
  });
});

function delta(concern: string): ReviewArchitectureDelta {
  return {
    status: "ready",
    summary: {
      material: true,
      changed_components: 1,
      changed_edges: 1,
      added_edges: 1,
      removed_edges: 0,
      modified_edges: 0,
      important_concerns: [concern],
      exactness: "snapshot",
      degraded: [],
    },
    graph: { nodes: [], focus: "" },
  } as unknown as ReviewArchitectureDelta;
}

describe("BriefArchitectureDelta", () => {
  it("does not publish a manual response after its loader is superseded", async () => {
    const stale = deferred<ReviewArchitectureDelta | null>();
    const loadStale = vi.fn(() => stale.promise);
    const loadCurrent = vi.fn(async () => delta("current response"));
    const onOpen = vi.fn();
    const { rerender } = render(<BriefArchitectureDelta eager={false} load={loadStale} onOpen={onOpen} />);

    await userEvent.click(screen.getByRole("button", { name: "Check architecture changes" }));
    expect(loadStale).toHaveBeenCalledTimes(1);

    rerender(<BriefArchitectureDelta eager={false} load={loadCurrent} onOpen={onOpen} />);
    await userEvent.click(await screen.findByRole("button", { name: "Check architecture changes" }));
    expect(await screen.findByText("current response")).toBeInTheDocument();

    await act(async () => {
      stale.resolve(delta("stale response"));
      await stale.promise;
    });

    expect(screen.queryByText("stale response")).toBeNull();
    expect(screen.getByText("current response")).toBeInTheDocument();
  });
});
