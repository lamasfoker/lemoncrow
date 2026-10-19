/**
 * Published feedback in the comment card: a comment on the provider is changed
 * through the provider; a failure is visible and the local comment unchanged;
 * an unpublished comment shows nothing (its edits stay local).
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, renderHook, screen, act, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const api = vi.hoisted(() => ({ fetchFeedbackSync: vi.fn(), syncPublishedComment: vi.fn() }));
vi.mock("./automationApi", () => ({
  fetchFeedbackSync: api.fetchFeedbackSync,
  syncPublishedComment: api.syncPublishedComment,
}));

import AnnotationCard from "./AnnotationCard";
import { FeedbackSyncProvider, useFeedbackSync, type FeedbackSyncContextValue } from "./feedbackSync";
import type { CommentSyncView } from "./automationApi";
import type { Annotation } from "./types";

function annotation(overrides: Partial<Annotation> = {}): Annotation {
  return {
    id: "ann-1",
    review_id: "rev-1",
    revision_id: "rrv-1",
    parent_id: "",
    kind: "comment",
    state: "open",
    body: "Rename this",
    created_by: "local",
    created_by_actor: "human",
    source: "human",
    source_id: "local",
    created_at: "",
    updated_at: "",
    anchor_method: "identical_blob",
    anchor_method_label: "file unchanged since the comment",
    anchor_exact: true,
    anchor_detail: "",
    anchored: true,
    path: "src/svc.py",
    side: "new",
    start_line: 3,
    end_line: 3,
    unit_key: "sym:abc",
    symbol: "",
    origin_symbol: "",
    ...overrides,
  };
}

const published: CommentSyncView = {
  published: {
    annotation_id: "ann-1",
    remote_ref: "github-comment:8000",
    kind: "comment",
    remote_id: 8000,
    target_ref: "github:acme/shop#42",
  },
  sync: null,
};

function failedSync(): CommentSyncView {
  return {
    ...published,
    sync: {
      id: "fs-1",
      subject_id: "ann-1",
      remote_ref: "github-comment:8000",
      remote_kind: "comment",
      provider: "github",
      op: "edit",
      state: "sync_failed",
      requested_body: "Rename this to total",
      local_body: "Rename this",
      error: "GitHub returned HTTP 502: bad gateway",
      attempts: 1,
      updated_at: "",
    },
  };
}

function mount(comment: Annotation, context: FeedbackSyncContextValue | null) {
  const card = (
    <AnnotationCard
      comments={[comment]}
      all={[comment]}
      label="Comment"
      composing={false}
      busy={false}
      onSubmit={vi.fn()}
      onCancel={vi.fn()}
      onResolve={vi.fn()}
      onReopen={vi.fn()}
    />
  );
  return render(context ? <FeedbackSyncProvider value={context}>{card}</FeedbackSyncProvider> : card);
}

function context(comments: Record<string, CommentSyncView>, sync = vi.fn().mockResolvedValue(true)): FeedbackSyncContextValue {
  return { comments, pending: "", errors: {}, sync };
}

describe("published feedback controls", () => {
  beforeEach(() => {
    api.fetchFeedbackSync.mockReset();
    api.syncPublishedComment.mockReset();
  });

  it("shows nothing for an unpublished comment: its edits stay local", () => {
    mount(annotation(), context({}));
    expect(screen.queryByTestId("published-feedback")).toBeNull();
    mount(annotation({ id: "ann-2" }), null);
    expect(screen.queryByTestId("published-feedback")).toBeNull();
  });

  it("edits and withdraws a published comment only through the provider", async () => {
    const sync = vi.fn().mockResolvedValue(true);
    mount(annotation(), context({ "ann-1": published }, sync));
    expect(screen.getByTestId("published-feedback")).toHaveTextContent("Published on GitHub");

    await userEvent.click(screen.getByRole("button", { name: "Edit on GitHub" }));
    const editor = screen.getByLabelText("Edited comment for GitHub");
    expect(screen.getByRole("button", { name: "Update on GitHub" })).toBeDisabled();
    await userEvent.clear(editor);
    await userEvent.type(editor, "Rename this to total");
    await userEvent.click(screen.getByRole("button", { name: "Update on GitHub" }));
    expect(sync).toHaveBeenCalledWith("ann-1", "edit", "Rename this to total");

    await userEvent.click(screen.getByRole("button", { name: "Withdraw from GitHub" }));
    expect(sync).toHaveBeenLastCalledWith("ann-1", "withdraw");
  });

  it("a folded comment is corrected by a follow-up", () => {
    mount(
      annotation(),
      context({ "ann-1": { ...published, published: { ...published.published!, kind: "review", remote_ref: "github-review:7001" } } }),
    );
    expect(screen.getByRole("button", { name: "Correct on GitHub" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Withdraw with a follow-up" })).toBeTruthy();
  });

  it("shows sync_failed visibly with the provider error and offers retry or discard", async () => {
    const sync = vi.fn().mockResolvedValue(true);
    mount(annotation(), context({ "ann-1": failedSync() }, sync));
    expect(screen.getByTestId("published-feedback")).toHaveTextContent("Sync to GitHub failed");
    expect(screen.getByRole("alert")).toHaveTextContent("GitHub returned HTTP 502: bad gateway The comment here is unchanged.");
    expect(screen.getByText("Rename this")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Edit on GitHub" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(sync).toHaveBeenCalledWith("ann-1", "retry");
    await userEvent.click(screen.getByRole("button", { name: "Discard change" }));
    expect(sync).toHaveBeenLastCalledWith("ann-1", "discard");
  });

  it("the hook reloads sync state after a sync and asks the Reader to reload a changed comment", async () => {
    api.fetchFeedbackSync
      .mockResolvedValueOnce({ review_id: "rev-1", comments: { "ann-1": published }, reviews: [] })
      .mockResolvedValueOnce({ review_id: "rev-1", comments: { "ann-1": failedSync() }, reviews: [] });
    api.syncPublishedComment.mockResolvedValueOnce({ sync: failedSync().sync, annotation_id: "ann-1", local_changed: false });
    const onChanged = vi.fn();
    const { result } = renderHook(() => useFeedbackSync({ reviewId: "rev-1", enabled: true, onChanged }));
    await waitFor(() => expect(result.current.comments["ann-1"]).toEqual(published));
    let ok = true;
    await act(async () => { ok = await result.current.sync("ann-1", "edit", "Rename this to total"); });
    expect(ok).toBe(false);
    expect(api.syncPublishedComment).toHaveBeenCalledWith("ann-1", "edit", "Rename this to total");
    expect(result.current.comments["ann-1"].sync?.state).toBe("sync_failed");
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("the hook surfaces a refused sync as an error on that comment", async () => {
    api.fetchFeedbackSync.mockResolvedValue({ review_id: "rev-1", comments: {}, reviews: [] });
    api.syncPublishedComment.mockRejectedValueOnce(new Error("only the comment's author can change it on the provider"));
    const { result } = renderHook(() => useFeedbackSync({ reviewId: "rev-1", enabled: true }));
    await act(async () => { await result.current.sync("ann-1", "withdraw"); });
    expect(result.current.errors["ann-1"]).toBe("only the comment's author can change it on the provider");
  });
});
