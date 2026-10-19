import { Button as UiButton } from "../components/ui/button";
import { useState } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Dialog, DialogContent, DialogTitle } from "../components/ui/dialog";
import ReviewCommandPalette, { type ReviewPaletteAction } from "./ReviewCommandPalette";
import readerSource from "./ReviewReader.tsx?raw";
import webPreviewSource from "./WebReviewPreview.tsx?raw";

function actions(): ReviewPaletteAction[] {
  return [
    { id: "refresh", label: "Refresh local revision", detail: "Accept latest source", run: vi.fn() },
    { id: "feedback", label: "Prepare feedback", detail: "Preview human feedback", run: vi.fn() },
    { id: "finish", label: "Finish review", run: vi.fn() },
  ];
}

describe("ReviewCommandPalette", () => {
  it("filters actions and executes the selected match with Enter", async () => {
    const rows = actions();
    const onClose = vi.fn();
    render(<ReviewCommandPalette mode="actions" actions={rows} onClose={onClose} />);

    const input = screen.getByRole("combobox", { name: "Filter review actions" });
    await waitFor(() => expect(document.activeElement).toBe(input));
    await userEvent.type(input, "feedback");

    expect(screen.queryByText("Refresh local revision")).toBeNull();
    expect(screen.getByText("Prepare feedback")).toBeTruthy();
    await userEvent.keyboard("{Enter}");

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(rows[1].run).toHaveBeenCalledTimes(1);
  });

  it("uses arrow keys to move the action selection", async () => {
    const rows = actions();
    render(<ReviewCommandPalette mode="actions" actions={rows} onClose={vi.fn()} />);

    const input = screen.getByRole("combobox", { name: "Filter review actions" });
    await waitFor(() => expect(document.activeElement).toBe(input));
    expect(input.getAttribute("aria-activedescendant")).toBe(screen.getByRole("option", { name: /Refresh local revision/ }).id);
    await userEvent.keyboard("{ArrowDown}");

    // The highlighted row is announced through the combobox while focus stays in the filter.
    const next = screen.getByRole("option", { name: /Prepare feedback/ });
    expect(next.getAttribute("aria-selected")).toBe("true");
    expect(input.getAttribute("aria-activedescendant")).toBe(next.id);
    await userEvent.keyboard("{Enter}");

    expect(rows[0].run).not.toHaveBeenCalled();
    expect(rows[1].run).toHaveBeenCalledTimes(1);
  });

  it("traps Tab inside the action dialog", async () => {
    const rows = actions();
    render(<ReviewCommandPalette mode="actions" actions={rows} onClose={vi.fn()} />);

    // Options are arrow-key rows (tabIndex -1), so the filter is the last Tab stop.
    const close = screen.getByRole("button", { name: "Close review commands" });
    const last = screen.getByRole("combobox", { name: "Filter review actions" });
    last.focus();
    await userEvent.tab();
    expect(document.activeElement).toBe(close);
    await userEvent.tab({ shift: true });
    expect(document.activeElement).toBe(last);
  });

  it("hands a dialog-opening action a usable dialog: focus inside it, Escape closes it", async () => {
    // The palette is a hand-off surface: an action can trade it for another dialog. The
    // user-visible contract is that the new dialog owns the screen — it is the modal the
    // opener's focus-restore guard can see, it holds focus, and Escape closes it.
    function Host() {
      const [paletteOpen, setPaletteOpen] = useState(true);
      const [historyOpen, setHistoryOpen] = useState(false);
      const rows: ReviewPaletteAction[] = [
        { id: "history", label: "Open review history", run: () => setHistoryOpen(true) },
        { id: "refresh", label: "Refresh local revision", run: vi.fn() },
      ];
      return (
        <>
          <UiButton unstyled type="button">Review actions</UiButton>
          {paletteOpen && (
            <ReviewCommandPalette mode="actions" actions={rows} onClose={() => setPaletteOpen(false)} />
          )}
          <Dialog open={historyOpen} onOpenChange={setHistoryOpen}>
            <DialogContent aria-describedby={undefined}>
              <DialogTitle>Review history</DialogTitle>
              <UiButton type="button">Revision 2</UiButton>
            </DialogContent>
          </Dialog>
        </>
      );
    }

    render(<Host />);
    const opener = screen.getByRole("button", { name: "Review actions", hidden: true });
    opener.focus();

    await userEvent.click(screen.getByRole("option", { name: /Open review history/ }));

    const overview = await screen.findByRole("dialog", { name: "Review history" });
    expect(screen.queryByRole("dialog", { name: "Review actions" })).toBeNull();
    // Exactly one modal is on screen, and it is the new dialog: this is the marker the
    // palette owner's focus-restore guard keys on before it re-focuses the opener.
    const modals = Array.from(document.querySelectorAll('[aria-modal="true"]'));
    expect(modals).toHaveLength(1);
    expect(modals[0]).toBe(overview);
    await waitFor(() => expect(overview.contains(document.activeElement)).toBe(true));
    expect(document.activeElement).not.toBe(opener);

    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Review history" })).toBeNull());
  });

  it("shows the complete keyboard model and closes with Escape", async () => {
    const onClose = vi.fn();
    render(<ReviewCommandPalette mode="shortcuts" actions={actions()} onClose={onClose} />);

    expect(screen.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeTruthy();
    for (const key of ["j / k", "J / K", "] / [", "r", "u", "x", "c", "s", "/", "f", "e", "p", "?", "1 / 2 / 3", "d / t / m", "{ / }"]) {
      expect(screen.getByText(key)).toBeTruthy();
    }
    expect(screen.getByText("Web preview")).toBeTruthy();
    expect(screen.getByText("Next / previous review item")).toBeTruthy();
    expect(screen.getAllByText("Esc")).toHaveLength(2);

    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("documents exactly the keys the Reader's keyboard handler acts on", () => {
    render(<ReviewCommandPalette mode="shortcuts" actions={[]} onClose={vi.fn()} />);
    const dialog = screen.getByRole("dialog", { name: "Keyboard shortcuts" });
    const documented = new Set(
      Array.from(dialog.querySelectorAll('[data-shortcut-scope="global"] kbd'))
        .flatMap((node) => (node.textContent ?? "").split(" / "))
        .map((key) => (key === "Esc" ? "Escape" : key)),
    );

    // The Reader's window keydown handler, read from source so a new binding
    // cannot ship without a help row (and a help row cannot outlive its binding).
    const start = readerSource.indexOf("function onKey(event: KeyboardEvent)");
    const end = readerSource.indexOf('window.addEventListener("keydown", onKey)', start);
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    const handler = readerSource.slice(start, end);
    const handled = new Set([
      ...Array.from(handler.matchAll(/case "([^"]+)":/g), (match) => match[1]),
      ...Array.from(handler.matchAll(/event\.key === "([^"]+)"/g), (match) => match[1]),
    ]);
    expect([...handled].sort()).toEqual([...documented].sort());

    const briefKeys = /const BRIEF_KEYS[^=]*= new Set\(\[(.*?)\]\);/.exec(readerSource)?.[1] ?? "";
    const brief = Array.from(briefKeys.matchAll(/"([^"]+)"/g), (match) => match[1]);
    expect(brief.length).toBeGreaterThan(0);
    for (const key of brief) expect(handled, `Brief key ${key}`).toContain(key);

    // Shortcut hints on palette actions name keys that really do that.
    const actionsStart = readerSource.indexOf("const reviewActions = useMemo");
    const actionsBlock = readerSource.slice(actionsStart, readerSource.indexOf("useEffect(", actionsStart));
    const hinted = Array.from(actionsBlock.matchAll(/shortcut: "([^"]+)"/g), (match) => match[1]);
    expect(hinted.length).toBeGreaterThan(0);
    for (const key of hinted) expect(handled, `palette hint ${key}`).toContain(key);
  });

  it("documents exactly the shortcuts handled by an active web preview", () => {
    render(<ReviewCommandPalette mode="shortcuts" actions={[]} onClose={vi.fn()} />);
    const dialog = screen.getByRole("dialog", { name: "Keyboard shortcuts" });
    const documented = new Set(
      Array.from(dialog.querySelectorAll('[data-shortcut-scope="preview"] kbd'))
        .flatMap((node) => (node.textContent ?? "").split(" / ")),
    );

    const start = webPreviewSource.indexOf("const onKey = (event: KeyboardEvent)");
    const end = webPreviewSource.indexOf('window.addEventListener("keydown", onKey)', start);
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    const handler = webPreviewSource.slice(start, end);
    const handled = new Set([
      ...Array.from(handler.matchAll(/key === "([^"]+)"/g), (match) => match[1]),
      ...Array.from(handler.matchAll(/event\.key === "([^"]+)"/g), (match) => match[1]),
    ]);
    expect([...handled].sort()).toEqual([...documented].sort());
  });
});
