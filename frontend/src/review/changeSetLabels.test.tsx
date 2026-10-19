/**
 * Phase 9R item 42 in the existing review surfaces.
 *
 * The change set rides the Review header metadata (state, members, deploy
 * order); its blocker is a server landing condition in the readiness sheet. A
 * member the viewer cannot read is counted, never named.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ChangeSetView } from "./changeSetApi";
import ReviewIdentityBar, { changeSetSummary } from "./ReviewIdentityBar";
import ReviewReadinessSheet, { deriveReviewReadiness } from "./ReviewReadinessSheet";
import type { ReviewLandingReadiness, ReviewOverview } from "./types";

const api = vi.hoisted(() => ({ fetchChangeSet: vi.fn() }));
vi.mock("./changeSetApi", () => api);

const API_ID = "a".repeat(32);
const WEB_ID = "b".repeat(32);

function view(overrides: Partial<ChangeSetView> = {}): ChangeSetView {
  return {
    available: true,
    linked: true,
    state: "blocked",
    members: [
      { alias: "m-web", readable: true, repository: "repo-web", review_id: WEB_ID, ref: `r/${WEB_ID}`, title: "Call invoices", status: "open", snapshot_number: 1, current: true, ready: true, blockers: 0, error: "" },
      { alias: "m-api", readable: true, repository: "repo-api", review_id: API_ID, ref: `r/${API_ID}`, title: "Serve invoices", status: "open", snapshot_number: 2, current: false, ready: false, blockers: 2, error: "" },
    ],
    member_count: 2,
    unreadable: 0,
    truncated: false,
    max_members: 8,
    ready_members: 1,
    contracts: { state: "compatible", issues: [], notes: [] },
    deploy_order: {
      state: "ordered",
      steps: [
        { position: 1, alias: "m-api", repository: "repo-api", review_id: API_ID, phase: "migrations", after: [] },
        { position: 2, alias: "m-api", repository: "repo-api", review_id: API_ID, phase: "code", after: [{ alias: "m-api", phase: "migrations", reason: "migrations before code" }] },
        { position: 3, alias: "m-web", repository: "repo-web", review_id: WEB_ID, phase: "code", after: [{ alias: "m-api", phase: "code", reason: "calls /invoices" }] },
      ],
      cycle: [],
    },
    ...overrides,
  };
}

function overview(changeSet: NonNullable<ReviewOverview["change"]>["change_set"]): ReviewOverview {
  return {
    session: { id: WEB_ID, ref: `r/${WEB_ID}`, status: "open", actor_type: "agent", repo_root: "/repo-web" },
    revision: { revision_number: 1 },
    title: "Call invoices",
    change_story: [],
    change: { ref: "", scm: [], relations: [], work_items: [], scm_requirements: [], change_set: changeSet },
  } as unknown as ReviewOverview;
}

const props = { focusMode: false, onShowOverview: vi.fn(), onShowHistory: vi.fn() };

describe("change set in the Review header (item 42)", () => {
  beforeEach(() => {
    api.fetchChangeSet.mockReset();
  });

  it("summarises the set state, each member's readiness and the deploy order", () => {
    const summary = changeSetSummary(view());
    expect(summary.label).toBe("Change set · 2 Changes · blocked");
    expect(summary.blocked).toBe(true);
    expect(summary.title).toContain(`repo-web r/${WEB_ID} Call invoices: this Change`);
    expect(summary.title).toContain(`repo-api r/${API_ID} Serve invoices: 2 blocker(s)`);
    expect(summary.title).toContain("Deploy order: 1. repo-api migrations → 2. repo-api code → 3. repo-web code");
    expect(changeSetSummary(view({ state: "ready" })).blocked).toBe(false);
  });

  it("names a contract break and a deploy-order cycle by readable repositories", () => {
    const summary = changeSetSummary(
      view({
        state: "needs_decision",
        contracts: { state: "incompatible", issues: [{ provider: "m-api", consumer: "m-web", contract: "route /invoices", paths: ["web/client.py"] }], notes: [] },
        deploy_order: { state: "needs_decision", steps: [], cycle: [{ alias: "m-api", phase: "code" }, { alias: "m-web", phase: "code" }] },
      }),
    );
    expect(summary.label).toBe("Change set · 2 Changes · deploy order needs a decision");
    expect(summary.title).toContain("repo-api removes route /invoices, which repo-web still uses.");
    expect(summary.title).toContain("Deploy order needs a decision: repo-api code, repo-web code depend on each other.");
    expect(summary.title).not.toContain("Deploy order: 1.");
  });

  it("counts a member the viewer cannot read without naming it", () => {
    const summary = changeSetSummary(
      view({
        state: "incomplete",
        members: [view().members[0], { alias: "", readable: false }],
        unreadable: 1,
        deploy_order: { state: "unknown", steps: [], cycle: [] },
      }),
    );
    expect(summary.label).toBe("Change set · 2 Changes · incomplete");
    expect(summary.title).toContain("1 member(s) you cannot read; the set is incomplete.");
    expect(summary.title).not.toContain("repo-api");
    expect(summary.title).not.toContain(API_ID);
    expect(summary.title).not.toContain("Deploy order");
  });

  it("promotes only blocked change sets in the header and stays silent for healthy or unlinked sets", async () => {
    api.fetchChangeSet.mockResolvedValue(view());
    const first = render(<ReviewIdentityBar overview={overview({ available: true, linked: true, links: 1 })} {...props} />);
    const label = await screen.findByTestId("review-change-set");
    expect(label.textContent).toBe("Change set · blocked");
    expect(api.fetchChangeSet).toHaveBeenCalledWith(WEB_ID);
    expect(label.getAttribute("aria-label")).toBe("Change set: Change set · blocked");
    expect(label.getAttribute("title")).toContain("Deploy order: 1. repo-api migrations");
    first.unmount();

    api.fetchChangeSet.mockReset();
    api.fetchChangeSet.mockResolvedValue(view({ state: "ready" }));
    const healthy = render(<ReviewIdentityBar overview={overview({ available: true, linked: true, links: 1 })} {...props} />);
    await waitFor(() => expect(api.fetchChangeSet).toHaveBeenCalledWith(WEB_ID));
    expect(screen.queryByTestId("review-change-set")).toBeNull();
    healthy.unmount();

    api.fetchChangeSet.mockClear();
    render(<ReviewIdentityBar overview={overview({ available: true, linked: false, links: 0 })} {...props} />);
    expect(screen.queryByTestId("review-change-set")).toBeNull();
    expect(api.fetchChangeSet).not.toHaveBeenCalled();
  });

  it("follows the live landing readiness and refetches the set when its conditions change", async () => {
    const landingWith = (hash: string, setState: string | null): ReviewLandingReadiness =>
      ({
        review_id: WEB_ID,
        conditions_hash: hash,
        conditions: setState
          ? [{ id: "change-set:members", label: "Change set members", state: setState === "ready" ? "pass" : "pending", detail: "", owner: "lemoncrow", category: "change_set", url: "", set_state: setState }]
          : [],
      }) as unknown as ReviewLandingReadiness;
    const linked = overview({ available: true, linked: true, links: 1 });
    // The fetched details are stale ("ready"), but the live readiness says blocked: the header follows readiness.
    api.fetchChangeSet.mockResolvedValue(view({ state: "ready" }));
    const bar = render(<ReviewIdentityBar overview={linked} landing={landingWith("h1", "blocked")} {...props} />);
    expect((await screen.findByTestId("review-change-set")).textContent).toBe("Change set · blocked");
    await waitFor(() => expect(api.fetchChangeSet).toHaveBeenCalledTimes(1));

    // The other member landed: readiness refreshes, the label clears and the details refetch.
    bar.rerender(<ReviewIdentityBar overview={linked} landing={landingWith("h2", "ready")} {...props} />);
    await waitFor(() => expect(api.fetchChangeSet).toHaveBeenCalledTimes(2));
    expect(screen.queryByTestId("review-change-set")).toBeNull();

    // Blocked again, then unlinked elsewhere: the header follows each refresh without reopening the Review.
    api.fetchChangeSet.mockResolvedValue(view({ state: "blocked" }));
    bar.rerender(<ReviewIdentityBar overview={linked} landing={landingWith("h3", "incomplete")} {...props} />);
    expect((await screen.findByTestId("review-change-set")).textContent).toBe("Change set · incomplete");
    api.fetchChangeSet.mockResolvedValue(view({ linked: false, state: "not_linked", members: [] }));
    bar.rerender(<ReviewIdentityBar overview={linked} landing={landingWith("h4", null)} {...props} />);
    await waitFor(() => expect(screen.queryByTestId("review-change-set")).toBeNull());
    expect(api.fetchChangeSet).toHaveBeenCalledTimes(4);
  });

  it("keeps the last readiness hash while readiness reloads and refetches only on a real change", async () => {
    const landingWith = (hash: string, reviewId = WEB_ID): ReviewLandingReadiness =>
      ({
        review_id: reviewId,
        conditions_hash: hash,
        conditions: [{ id: "change-set:members", label: "Change set members", state: "pending", detail: "", owner: "lemoncrow", category: "change_set", url: "", set_state: "blocked" }],
      }) as unknown as ReviewLandingReadiness;
    const linked = overview({ available: true, linked: true, links: 1 });
    api.fetchChangeSet.mockResolvedValue(view());
    // Mounted before readiness arrives: the details are fetched once.
    const bar = render(<ReviewIdentityBar overview={linked} landing={null} {...props} />);
    expect((await screen.findByTestId("review-change-set")).textContent).toBe("Change set · blocked");
    expect(api.fetchChangeSet).toHaveBeenCalledTimes(1);
    // The first readiness is no change, a reload (no landing while it loads) is none either,
    // nor is the same hash coming back.
    for (const landing of [landingWith("h1"), null, landingWith("h1"), null]) {
      bar.rerender(<ReviewIdentityBar overview={linked} landing={landing} {...props} />);
      await Promise.resolve();
      expect(screen.getByTestId("review-change-set").textContent).toBe("Change set · blocked");
    }
    // Another Review's readiness (a navigation still loading) is not this Change's.
    bar.rerender(<ReviewIdentityBar overview={linked} landing={landingWith("h9", API_ID)} {...props} />);
    await Promise.resolve();
    expect(api.fetchChangeSet).toHaveBeenCalledTimes(1);
    // A real change refetches, once.
    bar.rerender(<ReviewIdentityBar overview={linked} landing={landingWith("h2")} {...props} />);
    await waitFor(() => expect(api.fetchChangeSet).toHaveBeenCalledTimes(2));
    bar.rerender(<ReviewIdentityBar overview={linked} landing={null} {...props} />);
    bar.rerender(<ReviewIdentityBar overview={linked} landing={landingWith("h2")} {...props} />);
    await Promise.resolve();
    expect(api.fetchChangeSet).toHaveBeenCalledTimes(2);
  });

  it("keeps the details fetched on mount when the first readiness arrives before they do", async () => {
    let resolve: (value: ChangeSetView) => void = () => undefined;
    api.fetchChangeSet.mockReturnValue(new Promise<ChangeSetView>((done) => { resolve = done; }));
    const linked = overview({ available: true, linked: true, links: 1 });
    const bar = render(<ReviewIdentityBar overview={linked} landing={null} {...props} />);
    bar.rerender(
      <ReviewIdentityBar
        overview={linked}
        landing={{ review_id: WEB_ID, conditions_hash: "h1", conditions: [] } as unknown as ReviewLandingReadiness}
        {...props}
      />,
    );
    resolve(view());
    expect((await screen.findByTestId("review-change-set")).textContent).toBe("Change set · blocked");
    expect(api.fetchChangeSet).toHaveBeenCalledTimes(1);
  });

  it("names a repository holding two Changes of the set", () => {
    const summary = changeSetSummary(view({ duplicate_repositories: ["repo-api"] }));
    expect(summary.title).toContain("More than one Change of repo-api is in the set; unlink all but one.");
  });

  it("fails closed when the set cannot be loaded", async () => {
    api.fetchChangeSet.mockRejectedValue(new Error("boom"));
    render(<ReviewIdentityBar overview={overview({ available: true, linked: true, links: 1 })} {...props} />);
    const label = await screen.findByTestId("review-change-set");
    expect(label.getAttribute("title")).toContain("could not be loaded");
    expect(label.textContent).toBe("Change set unavailable");
  });
});

describe("change-set blocker in the readiness sheet (item 42)", () => {
  it("shows the set blocker and the deploy order as server landing conditions", () => {
    const model = deriveReviewReadiness({
      progress: { target_count: 1, reviewed: 1, changed_since_review: 0, needs_changes: 0, unreviewed: 0, unknown: 0, mechanical: 0 },
      openComments: 0,
      verification: { pass: 1, fail: 0, not_run: 0, unknown: 0 },
      outcome: null,
    });
    const condition = (id: string, label: string, state: "pass" | "pending", detail: string, reason = "") => ({
      id,
      label,
      state,
      detail,
      owner: "lemoncrow" as const,
      category: "change_set",
      url: "",
      reason,
    });
    const landing: ReviewLandingReadiness = {
      review_id: WEB_ID,
      snapshot_id: "rr-1",
      snapshot_number: 1,
      tree_fingerprint: "tree-1",
      source_fingerprint: "source-1",
      conditions: [
        condition("change-set:members", "Change set members", "pending", `Blocked by r/${API_ID.slice(0, 8)} in repo-api (2 blocker(s))`, "set_member_blocked"),
        condition("change-set:contracts", "Change set contracts", "pass", "Every contract one member provides or removes matches the other members' heads."),
        condition("change-set:deploy-order", "Change set deploy order", "pass", "Deploy order: repo-api migrations → repo-api code → repo-web code"),
      ],
      ready: false,
      armable: true,
      blocker_count: 1,
      local_blocker_count: 0,
      conditions_hash: "conditions-1",
      work_items: [],
      landing: { supported: true, provider: "github", state: "not_armed", armed: false, semantic_stale: false, revision_id: "", tree_fingerprint: "", conditions_hash: "", detail: "", url: "", mechanism: "" },
    };

    render(<ReviewReadinessSheet model={model} landing={landing} onClose={vi.fn()} />);

    expect(screen.getByText("Not ready · 1 condition")).toBeTruthy();
    expect(screen.getByText("Change set members")).toBeTruthy();
    expect(screen.getByText(/Blocked by r\/aaaaaaaa in repo-api \(2 blocker\(s\)\)/)).toBeTruthy();
    expect(screen.getByText("Deploy order: repo-api migrations → repo-api code → repo-web code")).toBeTruthy();
  });
});
