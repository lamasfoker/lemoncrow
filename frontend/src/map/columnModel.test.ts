import { describe, expect, it } from "vitest";

import { buildColumnModel, foldId, foldModules, stronglyConnected, type ColumnNode } from "./columnModel";

function module(id: string, extra: Partial<ColumnNode> = {}): ColumnNode {
  const slash = id.lastIndexOf("/");
  return { id, label: slash >= 0 ? id.slice(slash + 1) : id, parent: slash > 0 ? id.slice(0, slash) : "", files: 4, ...extra };
}

const OPTIONS = { width: 1200, maxCards: 40, expanded: new Set<string>() };

describe("buildColumnModel", () => {
  it("places every module left of what it depends on", () => {
    const model = buildColumnModel(
      [module("cli"), module("api"), module("core"), module("util")],
      [
        { source: "cli", target: "api", weight: 3 },
        { source: "api", target: "core", weight: 5 },
        { source: "cli", target: "core", weight: 1 },
        { source: "core", target: "util", weight: 2 },
      ],
      OPTIONS,
    );
    const column = (id: string) => model.cardById.get(id)!.column;
    expect([column("cli"), column("api"), column("core"), column("util")]).toEqual([0, 1, 2, 3]);
    for (const edge of model.edges) {
      expect(model.cardById.get(edge.from)!.x).toBeLessThan(model.cardById.get(edge.to)!.x);
    }
    expect(model.cycles).toEqual([]);
  });

  it("condenses a dependency cycle into one marked group in one column", () => {
    const model = buildColumnModel(
      [module("app"), module("a"), module("b"), module("base")],
      [
        { source: "app", target: "a", weight: 1 },
        { source: "a", target: "b", weight: 1 },
        { source: "b", target: "a", weight: 1 },
        { source: "b", target: "base", weight: 1 },
      ],
      OPTIONS,
    );
    expect(model.cycles).toHaveLength(1);
    expect(model.cycles[0].members.sort()).toEqual(["a", "b"]);
    expect(model.cardById.get("a")!.column).toBe(model.cardById.get("b")!.column);
    expect(model.cardById.get("a")!.cycle).not.toBeNull();
    expect(model.cardById.get("base")!.column).toBeGreaterThan(model.cardById.get("a")!.column);
    // Inside the cycle, cards are stacked without overlap.
    const [first, second] = model.cycles[0].members.map((id) => model.cardById.get(id)!).sort((p, q) => p.y - q.y);
    expect(second.y).toBeGreaterThanOrEqual(first.y + first.h);
  });

  it("puts modules with no links in their own trailing column", () => {
    const model = buildColumnModel([module("a"), module("b"), module("docs")], [{ source: "a", target: "b", weight: 1 }], OPTIONS);
    const unlinked = model.columns.find((column) => column.unlinked)!;
    expect(unlinked.index).toBe(model.cardById.get("docs")!.column);
    expect(unlinked.index).toBeGreaterThan(model.cardById.get("b")!.column);
  });

  it("folds sibling modules into their parent to keep the first frame within budget", () => {
    const nodes = [
      ...Array.from({ length: 30 }, (_, index) => module(`src/pkg/m${index}`, { changed: index === 3 ? 2 : 0 })),
      ...Array.from({ length: 20 }, (_, index) => module(`tests/t${index}`)),
      module("docs"),
    ];
    const edges = [{ source: "tests/t0", target: "src/pkg/m3", weight: 4 }];
    const model = buildColumnModel(nodes, edges, OPTIONS);
    expect(model.cards.length).toBeLessThanOrEqual(40);
    const fold = model.cardById.get(foldId("src/pkg"))!;
    expect(fold.fold).toBe(true);
    expect(fold.members).toHaveLength(30);
    expect(fold.changed).toBe(2);
    expect(model.cardOf.get("src/pkg/m3")).toBe(foldId("src/pkg"));
    // Links are re-pointed at the fold.
    expect(model.edges.some((edge) => edge.to === foldId("src/pkg"))).toBe(true);

    const opened = buildColumnModel(nodes, edges, { ...OPTIONS, expanded: new Set([foldId("src/pkg")]) });
    expect(opened.cardById.has("src/pkg/m3")).toBe(true);
    expect(opened.cardById.has(foldId("src/pkg"))).toBe(false);
  });

  it("is deterministic", () => {
    const nodes = Array.from({ length: 12 }, (_, index) => module(`m${index}`));
    const edges = Array.from({ length: 20 }, (_, index) => ({ source: `m${index % 12}`, target: `m${(index * 5 + 3) % 12}`, weight: 1 + (index % 3) }));
    const a = buildColumnModel(nodes, edges, OPTIONS);
    const b = buildColumnModel([...nodes].reverse(), [...edges].reverse(), OPTIONS);
    expect(a.cards.map((card) => [card.id, card.x, card.y])).toEqual(b.cards.map((card) => [card.id, card.x, card.y]));
  });
});

describe("helpers", () => {
  it("finds strongly connected components", () => {
    const out = new Map([
      ["a", new Set(["b"])],
      ["b", new Set(["c"])],
      ["c", new Set(["a"])],
      ["d", new Set(["a"])],
    ]);
    const components = stronglyConnected(["a", "b", "c", "d"], out).map((component) => component.join(","));
    expect(components.sort()).toEqual(["a,b,c", "d"]);
  });

  it("keeps a fold the reader opened", () => {
    const nodes = Array.from({ length: 6 }, (_, index) => module(`x/m${index}`));
    expect(new Set(foldModules(nodes, 2, new Set()).values())).toEqual(new Set([foldId("x")]));
    expect(new Set(foldModules(nodes, 2, new Set([foldId("x")])).values()).size).toBe(6);
  });
});
