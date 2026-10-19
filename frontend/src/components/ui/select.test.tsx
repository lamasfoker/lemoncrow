import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Select, SelectItem } from "./select";

describe("Select", () => {
  it("renders a custom listbox and changes value without a native select", async () => {
    const onValueChange = vi.fn();
    const user = userEvent.setup();
    const { container } = render(
      <Select value="review" onValueChange={onValueChange} aria-label="View mode">
        <SelectItem value="review">Review</SelectItem>
        <SelectItem value="source">Source</SelectItem>
      </Select>,
    );

    expect(container.querySelector("select")).toBeNull();
    await user.click(screen.getByRole("combobox", { name: "View mode" }));
    expect(screen.getByRole("listbox", { name: "View mode" })).toBeTruthy();
    await user.click(screen.getByRole("option", { name: "Source" }));
    expect(onValueChange).toHaveBeenCalledWith("source");
  });

  it("supports keyboard navigation and disabled items", async () => {
    const onValueChange = vi.fn();
    const user = userEvent.setup();
    render(
      <Select value="one" onValueChange={onValueChange} aria-label="Choice">
        <SelectItem value="one">One</SelectItem>
        <SelectItem value="two" disabled>Two</SelectItem>
        <SelectItem value="three">Three</SelectItem>
      </Select>,
    );

    const trigger = screen.getByRole("combobox", { name: "Choice" });
    trigger.focus();
    await user.keyboard("{ArrowDown}{ArrowDown}{Enter}");
    expect(onValueChange).toHaveBeenCalledWith("three");
  });
  it("filters searchable options and selects the filtered result", async () => {
    const onValueChange = vi.fn();
    const user = userEvent.setup();
    render(
      <Select value="main" onValueChange={onValueChange} aria-label="Project" searchable searchPlaceholder="Search projects">
        <SelectItem value="main" title="/repo">repo / main · main</SelectItem>
        <SelectItem value="feature" title="/other">repo / feature · feature/payments</SelectItem>
      </Select>,
    );

    await user.click(screen.getByRole("combobox", { name: "Project" }));
    const search = screen.getByRole("searchbox", { name: "Search projects" });
    expect(search).toHaveFocus();

    await user.type(search, "payments");
    expect(screen.queryByRole("option", { name: "repo / main · main" })).toBeNull();
    expect(screen.getByRole("option", { name: "repo / feature · feature/payments" })).toBeInTheDocument();

    await user.keyboard("{Enter}");
    expect(onValueChange).toHaveBeenCalledWith("feature");
  });

  it("closes on Tab and exposes the highlighted option to assistive tech", async () => {
    const user = userEvent.setup();
    render(
      <Select value="one" aria-label="Choice">
        <SelectItem value="one">One</SelectItem>
        <SelectItem value="two">Two</SelectItem>
      </Select>,
    );

    const trigger = screen.getByRole("combobox", { name: "Choice" });
    trigger.focus();
    await user.keyboard("{ArrowDown}{ArrowDown}");

    const highlighted = screen.getByRole("option", { name: "Two" });
    expect(trigger.getAttribute("aria-activedescendant")).toBe(highlighted.id);

    await user.keyboard("{Tab}");
    expect(screen.queryByRole("listbox", { name: "Choice" })).toBeNull();
  });

  it("opens above the trigger when there is not enough room below", async () => {
    const user = userEvent.setup();
    const originalInnerHeight = window.innerHeight;
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 600 });
    const rectSpy = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
      x: 20,
      y: 540,
      top: 540,
      left: 20,
      right: 220,
      bottom: 572,
      width: 200,
      height: 32,
      toJSON: () => ({}),
    } as DOMRect);

    try {
      render(
        <Select value="one" aria-label="Placement">
          <SelectItem value="one">One</SelectItem>
          <SelectItem value="two">Two</SelectItem>
        </Select>,
      );
      await user.click(screen.getByRole("combobox", { name: "Placement" }));
      screen.getByRole("listbox", { name: "Placement" });
      const popup = document.querySelector<HTMLElement>("[data-ui-select-popup]");
      expect(popup?.style.bottom).toBe("65px");
      expect(popup?.style.top).toBe("");
    } finally {
      rectSpy.mockRestore();
      Object.defineProperty(window, "innerHeight", { configurable: true, value: originalInnerHeight });
    }
  });

});
