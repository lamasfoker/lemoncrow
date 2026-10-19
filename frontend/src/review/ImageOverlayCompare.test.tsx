import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import ImageOverlayCompare from "./ImageOverlayCompare";

function renderCompare() {
  return render(
    <ImageOverlayCompare
      beforeSrc="before.png"
      afterSrc="after.png"
      beforeLabel="Before"
      afterLabel="After"
    />,
  );
}

describe("ImageOverlayCompare", () => {
  it("uses the divider itself as the drag handle and removes the initial hint after interaction", () => {
    renderCompare();
    const divider = screen.getByRole("slider", { name: "Screenshot comparison divider" }) as HTMLDivElement;
    const stage = divider.parentElement as HTMLDivElement;

    vi.spyOn(stage, "getBoundingClientRect").mockReturnValue({
      x: 100,
      y: 0,
      left: 100,
      top: 0,
      right: 500,
      bottom: 300,
      width: 400,
      height: 300,
      toJSON: () => ({}),
    });
    divider.setPointerCapture = vi.fn();
    divider.hasPointerCapture = vi.fn(() => true);
    divider.releasePointerCapture = vi.fn();

    expect(screen.getByTestId("image-overlay-drag-hint").textContent).toContain("Drag to compare");
    expect(screen.queryByRole("slider", { name: "Screenshot comparison position" })).toBeNull();

    const pointer = (type: string, clientX: number) => {
      const event = new MouseEvent(type, { bubbles: true, clientX });
      Object.defineProperty(event, "pointerId", { value: 1 });
      fireEvent(divider, event);
    };
    pointer("pointerdown", 300);
    pointer("pointermove", 400);
    pointer("pointerup", 400);

    expect(divider.getAttribute("aria-valuenow")).toBe("75");
    expect(divider.style.left).toBe("75%");
    expect(screen.queryByTestId("image-overlay-drag-hint")).toBeNull();
  });

  it("keeps the divider keyboard accessible", () => {
    renderCompare();
    const divider = screen.getByRole("slider", { name: "Screenshot comparison divider" });

    fireEvent.keyDown(divider, { key: "ArrowRight" });
    expect(divider.getAttribute("aria-valuenow")).toBe("51");
    expect(screen.queryByTestId("image-overlay-drag-hint")).toBeNull();

    fireEvent.keyDown(divider, { key: "End" });
    expect(divider.getAttribute("aria-valuenow")).toBe("100");
  });
});
