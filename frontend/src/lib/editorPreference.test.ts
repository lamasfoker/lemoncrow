import { afterEach, expect, it } from "vitest";
import { getEditorPreference, selectedEditor, setEditorPreference } from "./editorPreference";

const capability = {
  available: true,
  preferred: { id: "vscode", label: "VS Code" },
  editors: [{ id: "vscode", label: "VS Code" }, { id: "cursor", label: "Cursor" }],
};

afterEach(() => localStorage.removeItem("lemoncrow.preferredEditor"));

it("defaults to VS Code and persists a Cursor selection", () => {
  expect(getEditorPreference()).toBe("vscode");
  expect(selectedEditor(capability)?.id).toBe("vscode");
  setEditorPreference("cursor");
  expect(selectedEditor(capability)?.id).toBe("cursor");
});

it("uses an installed editor when the preference is unavailable", () => {
  expect(selectedEditor({ available: true, preferred: capability.editors[1], editors: [capability.editors[1]] })?.id).toBe("cursor");
});
