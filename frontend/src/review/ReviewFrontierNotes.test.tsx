import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ReviewFrontierNotes from "./ReviewFrontierNotes";

const api = vi.hoisted(() => ({ authenticatedFetch: vi.fn() }));
vi.mock("./reviewApi", () => ({ authenticatedFetch: api.authenticatedFetch }));

const props = { reviewId: "review/one", revisionId: "snapshot-a", rangeMode: "working_tree", historical: false };

describe("D6 frontier notes in the Reader", () => {
  beforeEach(() => { api.authenticatedFetch.mockReset(); });
  afterEach(() => { vi.clearAllMocks(); });

  it("surfaces an informational server note without claiming new authored work", async () => {
    api.authenticatedFetch.mockResolvedValue(Response.json({ notes: ["Review base changed since the previous capture (abc → def). Git ancestry was not verified."] }));
    render(<ReviewFrontierNotes {...props} />);
    const note = await screen.findByRole("note");
    expect(note.textContent).toContain("Git ancestry was not verified");
    expect(note.textContent).toContain("Base movement");
    expect(api.authenticatedFetch).toHaveBeenCalledWith("/api/reviews/review%2Fone/frontier");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("renders no note on an empty frontier or an old server without notes", async () => {
    api.authenticatedFetch.mockResolvedValueOnce(Response.json({ notes: [] }))
      .mockResolvedValueOnce(Response.json({ changed_since_review: [] }));
    const view = render(<ReviewFrontierNotes {...props} />);
    await waitFor(() => expect(api.authenticatedFetch).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId("review-frontier-notes")).toBeNull();
    view.rerender(<ReviewFrontierNotes {...props} revisionId="snapshot-b" />);
    await waitFor(() => expect(api.authenticatedFetch).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("drops an earlier Snapshot's notice when the revision changes", async () => {
    api.authenticatedFetch.mockResolvedValueOnce(Response.json({ notes: ["base changed"] }))
      .mockResolvedValueOnce(Response.json({ notes: [] }));
    const view = render(<ReviewFrontierNotes {...props} />);
    expect(await screen.findByText("base changed")).toBeTruthy();
    view.rerender(<ReviewFrontierNotes {...props} revisionId="snapshot-b" />);
    expect(screen.queryByText("base changed")).toBeNull();
    await waitFor(() => expect(api.authenticatedFetch).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("does not fetch for historical, commit-range or unavailable Review identity", async () => {
    const view = render(<ReviewFrontierNotes {...props} historical />);
    view.rerender(<ReviewFrontierNotes {...props} rangeMode="commit_range" />);
    view.rerender(<ReviewFrontierNotes {...props} reviewId="" />);
    expect(api.authenticatedFetch).not.toHaveBeenCalled();
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("degrades silently on an unavailable frontier", async () => {
    api.authenticatedFetch.mockRejectedValue(new Error("offline"));
    render(<ReviewFrontierNotes {...props} />);
    await waitFor(() => expect(api.authenticatedFetch).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("note")).toBeNull();
  });
});
