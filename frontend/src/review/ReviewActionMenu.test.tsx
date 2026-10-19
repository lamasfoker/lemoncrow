import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ReviewActionMenu from "./ReviewActionMenu";

type MenuProps = ComponentProps<typeof ReviewActionMenu>;

function renderMenu(overrides: Partial<MenuProps> = {}) {
  const props: MenuProps = {
    busy: false,
    readOnly: false,
    historical: false,
    allowRefresh: false,
    showFinish: false,
    reviewId: "abcdef12abcdef127abcdef12abcdef12",
    workspacePath: "",
    codeTheme: "github-dark",
    diffStyle: "split",
    diffOverflow: "scroll",
    contextOpen: false,
    focusMode: false,
    onRefresh: vi.fn(),
    onFinish: vi.fn(),
    onDownloadPatch: vi.fn(),
    onCodeThemeChange: vi.fn(),
    onDiffStyleChange: vi.fn(),
    onToggleDiffOverflow: vi.fn(),
    onToggleContext: vi.fn(),
    onToggleFocus: vi.fn(),
    onShowShortcuts: vi.fn(),
    onOpenFeedback: vi.fn(),
    ...overrides,
  };
  render(<ReviewActionMenu {...props} />);
  return props;
}

async function openMenu() {
  await userEvent.click(screen.getByRole("button", { name: "Review actions" }));
  return screen.findByRole("menu");
}

function setClipboard(writeText: ((value: string) => Promise<void>) | undefined) {
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: writeText ? { writeText } : undefined,
  });
}

afterEach(() => {
  Reflect.deleteProperty(navigator, "clipboard");
});

describe("ReviewActionMenu handoff", () => {
  it("no longer carries a send-to-agent entry: Send now and the verdict own the handoff", async () => {
    renderMenu();
    const menu = await openMenu();
    expect(menu.textContent).not.toMatch(/send to agent/i);
  });
});

describe("ReviewActionMenu", () => {
  it("copies the Change ID and keeps the menu open to confirm it", async () => {
    const writeText = vi.fn(async () => {});
    setClipboard(writeText);
    renderMenu();
    await openMenu();

    await userEvent.click(screen.getByRole("menuitem", { name: /Copy Change ID/ }));
    expect(writeText).toHaveBeenCalledWith("abcdef12abcdef127abcdef12abcdef12");
    expect(await screen.findByText("Copied")).toBeTruthy();
    expect(screen.getByRole("menu")).toBeTruthy();
  });

  it("says when the Change ID could not be copied", async () => {
    setClipboard(undefined);
    renderMenu();
    await openMenu();
    await userEvent.click(screen.getByRole("menuitem", { name: /Copy Change ID/ }));
    expect(await screen.findByText("Copy failed")).toBeTruthy();
  });

  it("copies the workspace path only when one is known", async () => {
    const writeText = vi.fn(async () => {});
    setClipboard(writeText);
    renderMenu({ workspacePath: "/home/dev/checkout" });
    await openMenu();

    const item = screen.getByRole("menuitem", { name: /Copy workspace path/ });
    expect(item.textContent).toContain("/home/dev/checkout");
    await userEvent.click(item);
    expect(writeText).toHaveBeenCalledWith("/home/dev/checkout");
    expect(await screen.findByText("Copied")).toBeTruthy();
  });

  it("omits the workspace path when none is known and reports a rejected write", async () => {
    setClipboard(vi.fn(async () => { throw new Error("denied"); }));
    renderMenu();
    await openMenu();
    expect(screen.queryByRole("menuitem", { name: /Copy workspace path/ })).toBeNull();
    await userEvent.click(screen.getByRole("menuitem", { name: /Copy Change ID/ }));
    expect(await screen.findByText("Copy failed")).toBeTruthy();
  });

  it("downloads the Snapshot patch", async () => {
    const props = renderMenu();
    await openMenu();
    const item = screen.getByRole("menuitem", { name: /Download Snapshot patch/ });
    expect(item.textContent).toContain("Apply with git apply");
    await userEvent.click(item);
    expect(props.onDownloadPatch).toHaveBeenCalledTimes(1);
  });

  it("changes the diff style from its submenu", async () => {
    const props = renderMenu({ diffStyle: "unified" });
    await openMenu();
    const diffView = screen.getByRole("menuitem", { name: /Diff view/ });
    expect(diffView.textContent).toContain("Unified");
    await userEvent.click(diffView);
    await userEvent.click(await screen.findByRole("menuitemradio", { name: "Auto · responsive" }));
    expect(props.onDiffStyleChange).toHaveBeenCalledWith("auto");
  });

  it("toggles line wrapping, showing the current mode and its w shortcut", async () => {
    const props = renderMenu({ diffOverflow: "wrap" });
    await openMenu();
    const item = screen.getByRole("menuitem", { name: /Code lines/ });
    expect(item.textContent).toContain("Wrap · w");
    await userEvent.click(item);
    expect(props.onToggleDiffOverflow).toHaveBeenCalledTimes(1);
    // Wrapping is a toggle the reviewer may flip repeatedly: the menu stays open.
    expect(screen.getByRole("menu")).toBeTruthy();
  });

  it.each([
    [{ contextOpen: false }, "Open context", "onToggleContext"],
    [{ contextOpen: true }, "Close context", "onToggleContext"],
    [{ focusMode: false }, "Enter focus mode", "onToggleFocus"],
    [{ focusMode: true }, "Exit focus mode", "onToggleFocus"],
    [{}, "Keyboard shortcuts", "onShowShortcuts"],
    [{}, "Send LemonCrow feedback", "onOpenFeedback"],
  ] as const)("with %o runs %s", async (overrides, name, handler) => {
    const props = renderMenu(overrides);
    await openMenu();
    await userEvent.click(screen.getByRole("menuitem", { name: new RegExp(name) }));
    expect(props[handler]).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  });

  it("lists the ? shortcut beside Keyboard shortcuts", async () => {
    renderMenu();
    await openMenu();
    expect(screen.getByRole("menuitem", { name: /Keyboard shortcuts/ }).textContent).toContain("?");
  });

  it("opens the GitHub publish preview from the menu for a pull-request Review", async () => {
    const onPublishGitHub = vi.fn();
    renderMenu({ onPublishGitHub });
    await openMenu();
    await userEvent.click(screen.getByRole("menuitem", { name: /Publish to GitHub/ }));
    expect(onPublishGitHub).toHaveBeenCalledTimes(1);
  });

  it("disables GitHub publish on a historical Snapshot", async () => {
    const onPublishGitHub = vi.fn();
    renderMenu({ onPublishGitHub, historical: true });
    await openMenu();
    const item = screen.getByRole("menuitem", { name: /Publish to GitHub/ });
    expect(item.getAttribute("aria-disabled")).toBe("true");
    await userEvent.click(item);
    expect(onPublishGitHub).not.toHaveBeenCalled();
  });

  it("has no GitHub publish entry for a Review that is not a pull request", async () => {
    renderMenu();
    await openMenu();
    expect(screen.queryByRole("menuitem", { name: /Publish to GitHub/ })).toBeNull();
  });
});
