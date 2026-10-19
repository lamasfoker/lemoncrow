import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const pdf = vi.hoisted(() => {
  const cancel = vi.fn();
  const render = vi.fn(() => ({ promise: Promise.resolve(), cancel }));
  const getPage = vi.fn(async (_page: number) => ({
    getViewport: () => ({ width: 420, height: 594 }),
    render,
  }));
  const documentProxy = { numPages: 2, getPage };
  const destroy = vi.fn();
  const getDocument = vi.fn(() => ({ promise: Promise.resolve(documentProxy), destroy }));
  const runtime = { loads: 0 };
  return {
    runtime,
    cancel,
    render,
    getPage,
    documentProxy,
    destroy,
    getDocument,
    GlobalWorkerOptions: { workerSrc: "" },
  };
});

const pdfModule = () => {
  pdf.runtime.loads += 1;
  return { getDocument: pdf.getDocument, GlobalWorkerOptions: pdf.GlobalWorkerOptions };
};

vi.mock("pdfjs-dist/build/pdf.worker.min.mjs?url", () => ({
  default: "/assets/pdf.worker.min.mjs",
}));

import type PdfReviewDocumentType from "./PdfReviewDocument";

let PdfReviewDocument: typeof PdfReviewDocumentType;

beforeEach(async () => {
  vi.clearAllMocks();
  vi.resetModules();
  pdf.runtime.loads = 0;
  vi.doMock("pdfjs-dist", pdfModule);
  PdfReviewDocument = (await import("./PdfReviewDocument")).default;
  pdf.GlobalWorkerOptions.workerSrc = "";
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({} as CanvasRenderingContext2D);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("PdfReviewDocument", () => {
  it("renders revision bytes in-app and navigates pages without an iframe", async () => {
    const blob = new Blob(["%PDF-demo"], { type: "application/pdf" });
    const { container } = render(<PdfReviewDocument blob={blob} label="After" />);

    await waitFor(() => expect(pdf.getDocument).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(pdf.getPage).toHaveBeenCalledWith(1));
    expect(pdf.GlobalWorkerOptions.workerSrc).toBe("/assets/pdf.worker.min.mjs");
    expect(container.querySelector("iframe")).toBeNull();
    expect(screen.getByText("1 / 2")).toBeTruthy();
    expect(screen.getByLabelText("After PDF page 1")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Next page of After" }));
    await waitFor(() => expect(pdf.getPage).toHaveBeenCalledWith(2));
    expect(screen.getByText("2 / 2")).toBeTruthy();
    expect(screen.getByLabelText("After PDF page 2")).toBeTruthy();
  });

  it("resets a rejected runtime import and re-initializes on Retry", async () => {
    vi.doMock("pdfjs-dist", () => {
      throw new Error("chunk load failed");
    });
    const blob = new Blob(["%PDF-demo"], { type: "application/pdf" });
    render(<PdfReviewDocument blob={blob} label="After" />);

    await screen.findByText(/PDF preview unavailable/);
    expect(pdf.getDocument).not.toHaveBeenCalled();

    vi.doMock("pdfjs-dist", pdfModule);
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(pdf.getPage).toHaveBeenCalledWith(1));
    expect(screen.queryByText(/PDF preview unavailable/)).toBeNull();
    expect(screen.getByText("1 / 2")).toBeTruthy();
  });

  it("shares one successful runtime across multiple PDFs", async () => {
    const blob = new Blob(["%PDF-a"], { type: "application/pdf" });
    const first = render(<PdfReviewDocument blob={blob} label="A" />);
    await waitFor(() => expect(pdf.getPage).toHaveBeenCalledWith(1));
    first.unmount();
    render(<PdfReviewDocument blob={new Blob(["%PDF-b"])} label="B" />);
    await waitFor(() => expect(pdf.getDocument).toHaveBeenCalledTimes(2));
    expect(pdf.runtime.loads).toBe(1);
  });
});
