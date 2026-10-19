/** Recognize browser and bundler errors caused by a failed lazy chunk import. */
export function isChunkLoadFailure(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  return error.name === "ChunkLoadError" ||
    /loading chunk .+ failed|failed to fetch dynamically imported module|error loading dynamically imported module|importing a module script failed|unable to preload css/i.test(error.message);
}
