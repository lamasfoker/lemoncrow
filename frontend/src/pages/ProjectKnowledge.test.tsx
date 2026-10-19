import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ requestBrowserJson: vi.fn() }));
vi.mock("../review/reviewApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../review/reviewApi")>()),
  requestBrowserJson: api.requestBrowserJson,
}));

import ProjectKnowledge from "./ProjectKnowledge";
import type { KnowledgeEntry, KnowledgeListing, KnowledgeOverview } from "./projectKnowledgeApi";

const REPO = `repository:${"a".repeat(64)}`;
const NARRATIVE = `knowledge:${"b".repeat(64)}`;
const DOCUMENT = `knowledge:${"c".repeat(64)}`;
const BASE = `/api/project-knowledge/${encodeURIComponent(REPO)}`;

afterEach(() => {
  vi.clearAllMocks();
});

function source() {
  return {
    provider: "git",
    repository_key: "acme/widgets",
    canonical_ref: "refs/heads/main",
    repo_root: "/work/widgets",
    updated_at: "2026-10-07T10:00:00Z",
  };
}

function listing(overrides: Partial<KnowledgeListing> = {}): KnowledgeListing {
  return {
    available: true,
    scope: "local",
    repositories: [
      {
        repository_id: REPO,
        canonical_ref: "refs/heads/main",
        current_generation: "generation:1",
        updated_at: "2026-10-07T10:00:00Z",
        entries: { deterministic: 3, source_document: 1, ai_narrated: 1 },
        source: source(),
      },
    ],
    truncated: false,
    sync_command: "lc knowledge project sync --repo <checkout>",
    legacy_overlay_note: "The legacy overlay is not Project Knowledge.",
    ...overrides,
  };
}

function entry(overrides: Partial<KnowledgeEntry> = {}): KnowledgeEntry {
  return {
    entry_id: NARRATIVE,
    subject: { kind: "path", key: "src/billing" },
    knowledge_kind: "subject_narrative",
    narrative: "subject",
    body: "Billing charges cards through the gateway.",
    body_truncated: false,
    state: "current",
    support: "ai_narrated",
    support_label: "AI narrative",
    source_refs: ["git:blob:src/billing/charge.py@abc"],
    source_refs_omitted: 2,
    source_paths: ["src/billing/charge.py"],
    source_paths_omitted: 0,
    review_refs: [],
    merge_refs: [],
    generation: "generation:1",
    event: { kind: "landed_change", label: "Landed change", generation_id: "generation:1", observed_at: "2026-10-06T09:00:00Z" },
    updated_at: "2026-10-07T10:00:00Z",
    ...overrides,
  };
}

function overview(overrides: Partial<KnowledgeOverview> = {}): KnowledgeOverview {
  return {
    repository: { repository_id: REPO, current_generation: "generation:1", source: source() },
    narratives: [entry()],
    narratives_truncated: false,
    context: {
      entries: [
        entry({
          entry_id: DOCUMENT,
          subject: { kind: "path", key: "docs/adr/0001.md" },
          knowledge_kind: "document",
          narrative: null,
          body: "We use the gateway for every charge.",
          support: "source_document",
          support_label: "Repository document",
          source_refs_omitted: 0,
          event: null,
        }),
      ],
      term: "",
      offset: 0,
      next_offset: 25,
    },
    evolution: [{ kind: "landed_change", label: "Landed change", generation_id: "generation:1", observed_at: "2026-10-06T09:00:00Z" }],
    regenerate: { available: true, reason: "" },
    ...overrides,
  };
}

function serve(routes: { overview?: KnowledgeOverview; listing?: KnowledgeListing } = {}) {
  api.requestBrowserJson.mockImplementation((path: string, init: RequestInit = {}) => {
    if (path === "/api/project-knowledge") return Promise.resolve(routes.listing ?? listing());
    if (path.startsWith(BASE) && !init.method) return Promise.resolve(routes.overview ?? overview());
    if (path.endsWith("/hide")) return Promise.resolve({ status: "hidden", entry_id: NARRATIVE });
    if (path.endsWith("/regenerate")) return Promise.resolve({ status: "queued", entry_id: NARRATIVE, enqueued: true });
    if (path.endsWith("/corrections")) {
      return Promise.resolve({
        path: "docs/project-knowledge/corrections/src-billing-0123456789.md",
        content: "# Correction\n",
        patch: "diff --git a/x b/x\n+# Correction\n",
      });
    }
    return Promise.reject(new Error(`unexpected ${path}`));
  });
}

function calls(predicate: (path: string, init: RequestInit) => boolean) {
  return api.requestBrowserJson.mock.calls.filter(([path, init = {}]) => predicate(String(path), init as RequestInit));
}

describe("ProjectKnowledge", () => {
  it("names each kind of knowledge and shows the sync command when nothing is synced", async () => {
    serve({ listing: listing({ repositories: [] }) });
    render(<ProjectKnowledge />);

    expect(await screen.findByText("Nothing synced yet")).toBeInTheDocument();
    expect(screen.getByText("lc knowledge project sync --repo <checkout>")).toBeInTheDocument();
    for (const term of ["Project Knowledge", "Agent Memory", "Playbooks", "Review rules"]) {
      expect(within(screen.getByLabelText("What each kind of knowledge is")).getByText(term)).toBeInTheDocument();
    }
    expect(screen.getByText("The legacy overlay is not Project Knowledge.")).toBeInTheDocument();
    // Reading never asks for a repository it does not have.
    expect(calls((path) => path !== "/api/project-knowledge")).toHaveLength(0);
  });

  it("shows narratives with their sources, source-backed context and recent evolution", async () => {
    serve();
    render(<ProjectKnowledge />);

    expect(await screen.findByText("Billing charges cards through the gateway.")).toBeInTheDocument();
    expect(screen.getByText("We use the gateway for every charge.")).toBeInTheDocument();
    expect(screen.getByText("acme/widgets")).toBeInTheDocument();
    const narratives = screen.getByRole("region", { name: "AI narratives" });
    expect(within(narratives).getByText("AI narrative")).toBeInTheDocument();
    expect(within(narratives).getByText("git:blob:src/billing/charge.py@abc")).toBeInTheDocument();
    expect(within(narratives).getByText("and 2 more")).toBeInTheDocument();
    const context = screen.getByRole("region", { name: "Source-backed context" });
    expect(within(context).getByText("Repository document")).toBeInTheDocument();
    expect(within(context).queryByRole("button", { name: "Hide" })).not.toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Recent evolution" })).getByText("Landed change")).toBeInTheDocument();
    expect(calls((_path, init) => Boolean(init.method))).toHaveLength(0);
  });

  it("hides a narrative, refreshes, and offers a fresh one", async () => {
    serve();
    render(<ProjectKnowledge />);
    const narratives = await screen.findByRole("region", { name: "AI narratives" });
    await within(narratives).findByText("Billing charges cards through the gateway.");

    fireEvent.click(within(narratives).getByRole("button", { name: "Hide" }));
    const notice = await screen.findByTestId("project-knowledge-notice");
    expect(notice).toHaveTextContent("no longer feeds Brief, Ask or AI Review");
    expect(calls((path, init) => path === `${BASE}/narratives/${encodeURIComponent(NARRATIVE)}/hide` && init.method === "POST")).toHaveLength(1);
    await waitFor(() => expect(calls((path, init) => path.startsWith(BASE) && !init.method)).toHaveLength(2));

    fireEvent.click(within(notice).getByRole("button", { name: "Regenerate a fresh narrative" }));
    await waitFor(() => expect(screen.getByTestId("project-knowledge-notice")).toHaveTextContent("Queued."));
    // Gate K: local work runs when the local worker runs it, never "always".
    expect(screen.getByTestId("project-knowledge-notice")).toHaveTextContent("when LemonCrow's local worker runs it");
    expect(
      calls((path, init) => path === `${BASE}/narratives/${encodeURIComponent(NARRATIVE)}/regenerate` && init.method === "POST"),
    ).toHaveLength(1);
  });

  it("keeps Regenerate unavailable, with the reason, when the checkout is unknown", async () => {
    serve({ overview: overview({ regenerate: { available: false, reason: "run sync from the checkout once" } }) });
    render(<ProjectKnowledge />);
    const narratives = await screen.findByRole("region", { name: "AI narratives" });
    const regenerate = await within(narratives).findByRole("button", { name: "Regenerate" });

    expect(regenerate).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(regenerate);
    expect(calls((path) => path.endsWith("/regenerate"))).toHaveLength(0);
  });

  it("reports a refused regenerate instead of claiming it queued", async () => {
    serve();
    render(<ProjectKnowledge />);
    const narratives = await screen.findByRole("region", { name: "AI narratives" });
    await within(narratives).findByText("Billing charges cards through the gateway.");
    api.requestBrowserJson.mockImplementationOnce(() =>
      Promise.resolve({ status: "refused", reason: "content_access_unavailable", entry_id: NARRATIVE }),
    );

    fireEvent.click(within(narratives).getByRole("button", { name: "Regenerate" }));
    expect(await screen.findByTestId("project-knowledge-notice")).toHaveTextContent(
      "Not queued: content_access_unavailable.",
    );
  });

  it("turns a correction into a patch and writes nothing itself", async () => {
    serve();
    render(<ProjectKnowledge />);
    const narratives = await screen.findByRole("region", { name: "AI narratives" });
    await within(narratives).findByText("Billing charges cards through the gateway.");

    fireEvent.click(within(narratives).getByRole("button", { name: "Propose correction" }));
    const form = within(narratives).getByTestId("project-knowledge-correction");
    expect(within(form).getByRole("button", { name: "Create patch" })).toBeDisabled();
    fireEvent.change(within(form).getByLabelText("Correction"), { target: { value: "Refunds go through the ledger." } });
    fireEvent.click(within(form).getByRole("button", { name: "Create patch" }));

    expect(await within(form).findByText("docs/project-knowledge/corrections/src-billing-0123456789.md")).toBeInTheDocument();
    const [request] = calls((path) => path.endsWith("/corrections"));
    expect(request[0]).toBe(`${BASE}/corrections`);
    expect(JSON.parse(String((request[1] as RequestInit).body))).toEqual({
      text: "Refunds go through the ledger.",
      entry_id: NARRATIVE,
    });
  });

  it("searches and pages the source-backed context", async () => {
    serve();
    render(<ProjectKnowledge />);
    const context = await screen.findByRole("region", { name: "Source-backed context" });
    await within(context).findByText("We use the gateway for every charge.");

    fireEvent.change(within(context).getByLabelText("Search Project Knowledge"), { target: { value: " billing " } });
    fireEvent.click(within(context).getByRole("button", { name: "Search" }));
    await waitFor(() => expect(calls((path) => path === `${BASE}?q=billing`)).toHaveLength(1));

    fireEvent.click(await within(context).findByRole("button", { name: "More" }));
    await waitFor(() => expect(calls((path) => path === `${BASE}?offset=25`)).toHaveLength(1));
  });

  it("shows a failed read as an alert", async () => {
    api.requestBrowserJson.mockRejectedValue(new Error("Project Knowledge is unavailable"));
    render(<ProjectKnowledge />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Project Knowledge is unavailable");
  });
});
