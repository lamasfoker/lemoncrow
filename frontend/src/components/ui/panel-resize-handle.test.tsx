import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { PanelResizeHandle } from "./panel-resize-handle";

function setup() {
  const onResize = vi.fn();
  const { container } = render(<aside><PanelResizeHandle label="Resize panel" onResize={onResize} /></aside>);
  vi.spyOn(container.firstElementChild!, "getBoundingClientRect").mockReturnValue({ width: 270 } as DOMRect);
  const handle = screen.getByRole("button", { name: "Resize panel" });
  handle.setPointerCapture = vi.fn();
  handle.hasPointerCapture = vi.fn(() => true);
  handle.releasePointerCapture = vi.fn();
  return { handle, onResize };
}

describe("PanelResizeHandle", () => {
  it("resizes with the keyboard and clamps both width limits", () => {
    const { handle, onResize } = setup();
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(onResize).toHaveBeenLastCalledWith(290);
    const panel = handle.parentElement!;
    vi.spyOn(panel, "getBoundingClientRect").mockReturnValue({ width: 180 } as DOMRect);
    fireEvent.keyDown(handle, { key: "ArrowLeft" });
    expect(onResize).toHaveBeenLastCalledWith(180);
    vi.spyOn(panel, "getBoundingClientRect").mockReturnValue({ width: 600 } as DOMRect);
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(onResize).toHaveBeenLastCalledWith(Math.round(Math.min(520, window.innerWidth * 0.45)));
  });

  it("drags from the starting width and stops when capture is lost", () => {
    const { handle, onResize } = setup();
    // jsdom has no PointerEvent constructor.
    vi.stubGlobal("PointerEvent", MouseEvent);
    fireEvent.pointerDown(handle, { button: 0, clientX: 270 });
    fireEvent.pointerMove(handle, { clientX: 350 });
    expect(onResize).toHaveBeenLastCalledWith(350);
    fireEvent.lostPointerCapture(handle);
    onResize.mockClear();
    fireEvent.pointerMove(handle, { clientX: 400 });
    expect(onResize).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});
