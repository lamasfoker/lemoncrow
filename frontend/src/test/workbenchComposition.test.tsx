import { fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Button, WorkbenchApp, type PageComposition } from "@lemoncrow/frontend";
import { TimeRangeProvider } from "../lib/TimeRangeContext";

vi.mock("../pages/Home", () => ({ default: () => <div>Shared Home</div> }));
vi.mock("../pages/Usage", () => ({ default: () => <div>Shared Usage</div> }));
vi.mock("../review/ReviewPreparation", () => ({ default: () => null }));

afterEach(() => vi.restoreAllMocks());

function open(path: string, pageComposition?: PageComposition) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <TimeRangeProvider>
        <WorkbenchApp pageComposition={pageComposition} />
      </TimeRangeProvider>
    </MemoryRouter>,
  );
}

describe("shared workbench package composition", () => {
  it("uses shared defaults without importing a private composition", async () => {
    open("/usage");
    expect(await screen.findByText("Shared Usage")).toBeTruthy();
    expect(screen.getByRole("link", { name: /Code/ })).toBeTruthy();
    expect(screen.getByRole("link", { name: /Activity/ })).toBeTruthy();
  });

  it("replaces one page and retains other shared pages and navigation", async () => {
    function CustomUsage() {
      const [count, setCount] = useState(0);
      return <Button onClick={() => setCount(count + 1)}>Custom Usage {count}</Button>;
    }
    open("/usage", { Usage: CustomUsage });
    fireEvent.click(await screen.findByRole("button", { name: "Custom Usage 0" }));
    expect(screen.getByRole("button", { name: "Custom Usage 1" })).toBeTruthy();
    fireEvent.click(screen.getByRole("link", { name: /Home/ }));
    expect(await screen.findByText("Shared Home")).toBeTruthy();
  });

  it("keeps an override scoped to its own app instance", async () => {
    const custom = open("/usage", { Usage: () => <div>Custom Usage</div> });
    const local = open("/usage");
    expect(await within(custom.container).findByText("Custom Usage")).toBeTruthy();
    expect(await within(local.container).findByText("Shared Usage")).toBeTruthy();
    expect(within(local.container).queryByText("Custom Usage")).toBeNull();
  });
});
