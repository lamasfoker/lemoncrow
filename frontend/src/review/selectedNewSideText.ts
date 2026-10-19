/**
 * Read new-side lines already present in a revision-pinned unified diff.
 * A range crossing collapsed context is incomplete and must be fetched.
 */
export function selectedNewSideTextFromPatch(patch: string, startLine: number, endLine: number): string | null {
  if (startLine < 1 || endLine < startLine) return null;
  const lines = patch.split("\n");
  const selected = new Map<number, { text: string; newline: boolean }>();
  let newLine = 0;
  let inHunk = false;
  let lastNewLine: number | null = null;

  for (const line of lines) {
    const header = /^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
    if (header) {
      newLine = Number(header[1]);
      inHunk = true;
      lastNewLine = null;
      continue;
    }
    if (!inHunk) continue;
    if (line.startsWith("diff --git ")) {
      inHunk = false;
      continue;
    }
    if (line.startsWith("\\ No newline at end of file")) {
      if (lastNewLine !== null) {
        const previous = selected.get(lastNewLine);
        if (previous) previous.newline = false;
      }
      continue;
    }
    if (line.startsWith("-")) {
      lastNewLine = null;
      continue;
    }
    if (line.startsWith("+") || line.startsWith(" ")) {
      if (newLine >= startLine && newLine <= endLine) {
        selected.set(newLine, { text: line.slice(1), newline: true });
      }
      lastNewLine = newLine;
      newLine += 1;
      continue;
    }
    // Unknown diff content cannot establish the exact selected source.
    inHunk = false;
  }

  let text = "";
  for (let line = startLine; line <= endLine; line += 1) {
    const entry = selected.get(line);
    if (!entry) return null;
    text += entry.text + (entry.newline ? "\n" : "");
  }
  return text;
}
