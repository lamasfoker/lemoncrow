import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NativeTitleTooltipBridge } from "./tooltip";

afterEach(() => {
  vi.useRealTimers();
});

describe("NativeTitleTooltipBridge", () => {
  it("replaces a native title hint while hovered and restores it on leave", () => {
    vi.useFakeTimers();
    render(
      <>
        <NativeTitleTooltipBridge delayDuration={0} />
        <button title="Full file path">file.tsx</button>
      </>
    );

    const button = screen.getByRole("button", { name: "file.tsx" });
    fireEvent.pointerOver(button);

    expect(button).not.toHaveAttribute("title");
    act(() => vi.runAllTimers());
    expect(screen.getByRole("tooltip")).toHaveTextContent("Full file path");

    fireEvent.pointerOut(button, { relatedTarget: document.body });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    expect(button).toHaveAttribute("title", "Full file path");
  });

  it("also replaces title hints reached by keyboard focus", () => {
    vi.useFakeTimers();
    render(
      <>
        <NativeTitleTooltipBridge delayDuration={0} />
        <button title="Copy path">Copy</button>
      </>
    );

    const button = screen.getByRole("button", { name: "Copy" });
    fireEvent.focusIn(button);
    act(() => vi.runAllTimers());

    expect(button).not.toHaveAttribute("title");
    expect(screen.getByRole("tooltip")).toHaveTextContent("Copy path");

    fireEvent.focusOut(button, { relatedTarget: document.body });
    expect(button).toHaveAttribute("title", "Copy path");
  });

  it("does not repurpose semantic iframe titles", () => {
    render(
      <>
        <NativeTitleTooltipBridge delayDuration={0} />
        <iframe title="Preview frame" />
      </>
    );

    const frame = screen.getByTitle("Preview frame");
    fireEvent.pointerOver(frame);

    expect(frame).toHaveAttribute("title", "Preview frame");
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });
});
