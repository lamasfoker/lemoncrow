import type { CodeMapEditorCapability, CodeMapEditorDescriptor } from "../api";

const STORAGE_KEY = "lemoncrow.preferredEditor";

export type EditorId = "vscode" | "cursor";

export function getEditorPreference(): EditorId {
  try {
    return localStorage.getItem(STORAGE_KEY) === "cursor" ? "cursor" : "vscode";
  } catch {
    return "vscode";
  }
}

export function setEditorPreference(editor: EditorId): void {
  localStorage.setItem(STORAGE_KEY, editor);
}

export function selectedEditor(capability: CodeMapEditorCapability): CodeMapEditorDescriptor | null {
  return capability.editors.find((editor) => editor.id === getEditorPreference()) ?? capability.preferred;
}
