/**
 * Reader links come from SCM/work-item relationships and provider payloads.
 * Only absolute web URLs are ever rendered as clickable hrefs.
 */
export function webHref(url: string | null | undefined): string | undefined {
  const value = (url ?? "").trim();
  if (!value) return undefined;
  try {
    const parsed = new URL(value);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? value : undefined;
  } catch {
    return undefined;
  }
}
