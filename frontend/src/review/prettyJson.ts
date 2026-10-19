/**
 * Indented JSON for display, or the original text when it is not JSON or when
 * re-serializing would change a number (an integer beyond 2^53, or one that
 * overflows to Infinity). Evidence must show the bytes that were produced.
 */
export function prettyJsonText(text: string): string {
  let lossy = false;
  try {
    const parsed: unknown = JSON.parse(text, (_key, value: unknown) => {
      if (typeof value === "number" && (!Number.isFinite(value) || (Number.isInteger(value) && !Number.isSafeInteger(value)))) {
        lossy = true;
      }
      return value;
    });
    return lossy ? text : JSON.stringify(parsed, null, 2);
  } catch {
    return text;
  }
}
