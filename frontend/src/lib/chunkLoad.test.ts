import { isChunkLoadFailure } from "./chunkLoad";

describe("isChunkLoadFailure", () => {
  it.each([
    "Loading chunk 12 failed.",
    "Failed to fetch dynamically imported module: https://example.com/Reader.js",
    "error loading dynamically imported module",
    "Importing a module script failed.",
    "Unable to preload CSS for /assets/Reader.css",
  ])("recognizes chunk failures: %s", (message) => {
    expect(isChunkLoadFailure(new TypeError(message))).toBe(true);
  });

  it("recognizes a named ChunkLoadError", () => {
    const error = new Error("network unavailable");
    error.name = "ChunkLoadError";
    expect(isChunkLoadFailure(error)).toBe(true);
  });

  it.each([new Error("Reader failed to render"), new TypeError("Failed to fetch"), null, "Loading chunk 12 failed."])(
    "does not classify unrelated failures as chunk failures",
    (error) => {
      expect(isChunkLoadFailure(error)).toBe(false);
    },
  );
});
