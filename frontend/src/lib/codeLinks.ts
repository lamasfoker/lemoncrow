/**
 * Links between Review and Code. Both surfaces name a location the same way
 * (repository-relative path plus line range), and each link carries `from`, the
 * exact location to come back to, so one Back trail spans both.
 */

/** A file (line 0) or a line range to open in Code, which shows the working tree. */
export interface CodeTarget {
  path: string;
  line: number;
  endLine: number;
}

/** URL param holding where Back returns. */
export const RETURN_PARAM = "from";

/** An in-app path that is safe to navigate back to, or "" for anything else. */
export function safeReturnPath(value: string | null | undefined): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.startsWith("/\\")) return "";
  return value;
}

/**
 * The current location as a Back target. Its own `from` is dropped: the trail
 * is one step back to where the reader came from, and a link chain that
 * nested every hop would grow without bound.
 */
export function currentReturnPath(location: { pathname: string; search: string } = window.location): string {
  const params = new URLSearchParams(location.search);
  params.delete(RETURN_PARAM);
  const query = params.toString();
  return `${location.pathname}${query ? `?${query}` : ""}`;
}

/** `path` with `from` set to `returnTo`. */
export function withReturnPath(path: string, returnTo: string): string {
  const url = new URL(path, "http://in-app.invalid");
  if (safeReturnPath(returnTo)) url.searchParams.set(RETURN_PARAM, returnTo);
  return `${url.pathname}${url.search}${url.hash}`;
}

/** The `/code` URL that opens `target` in the checkout at `repo`. */
export function codeHref(repo: string, target: CodeTarget, returnTo: string): string {
  const params = new URLSearchParams({ repo, file: target.path, view: "source" });
  if (target.line > 0) {
    params.set("line", String(target.line));
    if (target.endLine > target.line) params.set("end", String(target.endLine));
  }
  return withReturnPath(`/code?${params.toString()}`, returnTo);
}

/** The Back target a page was opened with, if it is safe to follow. */
export function returnPathFrom(params: URLSearchParams): string {
  return safeReturnPath(params.get(RETURN_PARAM));
}
