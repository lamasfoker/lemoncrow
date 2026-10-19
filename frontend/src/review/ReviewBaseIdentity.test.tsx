import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import ReviewBaseIdentity from "./ReviewBaseIdentity";
import type { ReviewOverview } from "./types";

function overview(rangeMode: string, recordedBase?: string, effectiveBase = "feedabcd1234567890"): ReviewOverview {
  return {
    session: { range_mode: rangeMode, review_base_sha: recordedBase },
    revision: { base_sha: effectiveBase },
  } as ReviewOverview;
}

describe("Review base identity", () => {
  it("shows the recorded Review base and effective Snapshot base when frozen", () => {
    render(<ReviewBaseIdentity overview={overview("working_tree", "1234567890abcdef", "fedcba9876543210")} />);
    const detail = screen.getByTestId("review-base-identity");
    expect(detail.textContent).toContain("Review base1234567890ab");
    expect(detail.textContent).toContain("Snapshot basefedcba987654");
    expect(detail.getAttribute("title")).toContain("Recorded Review base 1234567890abcdef");
    expect(detail.getAttribute("title")).toContain("Effective Snapshot base fedcba9876543210");
    expect(detail.textContent).not.toContain("Base not frozen");
  });

  it.each(["working_tree", "staged"])("names unfrozen %s Reviews, including old clients, detached and unbound fallback", (mode) => {
    render(<ReviewBaseIdentity overview={overview(mode, "")} />);
    const detail = screen.getByTestId("review-base-identity");
    expect(detail.textContent).toContain("Base not frozen");
    expect(detail.textContent).toContain("Snapshot basefeedabcd1234");
    expect(detail.getAttribute("title")).toContain("older client or detached checkout");
  });

  it("treats an omitted recorded base (legacy server) as unfrozen", () => {
    render(<ReviewBaseIdentity overview={overview("working_tree")} />);
    expect(screen.getByText("Base not frozen")).toBeTruthy();
  });

  it("does not apply mutable base semantics to commit-range Reviews", () => {
    render(<ReviewBaseIdentity overview={overview("commit_range")} />);
    expect(screen.queryByTestId("review-base-identity")).toBeNull();
  });
});
