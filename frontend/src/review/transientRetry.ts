function statusOf(error: unknown): number | null {
  const status = Number((error as { status?: unknown } | null)?.status);
  return Number.isFinite(status) ? status : null;
}

/**
 * A failure worth retrying: no HTTP response at all (the browser's "Failed to
 * fetch" when the local server restarts or resets the connection) or a
 * transient status. Other 4xx answers are permanent for this request.
 */
export function transientRequestFailure(error: unknown): boolean {
  const status = statusOf(error);
  return status === null || status === 409 || status === 429 || status >= 500;
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => globalThis.setTimeout(resolve, ms));
}

/** Runs `run`, retrying transient failures once per entry in `delaysMs`. */
export async function withTransientRetry<T>(run: () => Promise<T>, delaysMs: readonly number[] = [300, 1200]): Promise<T> {
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await run();
    } catch (error) {
      const wait = delaysMs[attempt];
      if (wait === undefined || !transientRequestFailure(error)) throw error;
      await delay(wait);
    }
  }
}
