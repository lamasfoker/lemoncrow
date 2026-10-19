// Monaco for the /code source viewer only, loaded on demand by ReadOnlyCodeViewer.
// Every other editor surface (Review proposals etc.) stays on CodeMirror 6.
import * as monaco from "monaco-editor/editor";
import "monaco-editor/features/register.all";
import "monaco-editor/languages/definitions/register.all";
import "monaco-editor/languages/features/json/register.js";
import EditorWorker from "monaco-editor/editor/editor.worker.js?worker";
import JsonWorker from "monaco-editor/language/json/json.worker.js?worker";

import "./monacoViewer.css";

globalThis.MonacoEnvironment = {
  getWorker: (_workerId: string, label: string) => (label === "json" ? new JsonWorker() : new EditorWorker()),
};

// Monaco ships no TOML grammar; INI's `[section]` / `key = value` is close enough to read.
monaco.languages.register({ id: "ini", extensions: [".toml"] });

// Backgrounds match --surface-code in index.css so sticky scroll and the minimap blend in.
monaco.editor.defineTheme("lc-dark", {
  base: "vs-dark",
  inherit: true,
  rules: [],
  colors: {
    "editor.background": "#080808",
    "editorGutter.background": "#080808",
    "editorStickyScroll.background": "#080808",
    "editorLineNumber.foreground": "#737373",
    "editorLineNumber.activeForeground": "#a3a3a3",
    "editor.selectionBackground": "#525252a6",
    "editor.inactiveSelectionBackground": "#52525266",
  },
});
monaco.editor.defineTheme("lc-light", {
  base: "vs",
  inherit: true,
  rules: [],
  colors: {
    "editor.background": "#f7f7f8",
    "editorGutter.background": "#f7f7f8",
    "editorStickyScroll.background": "#f7f7f8",
    "editorLineNumber.foreground": "#a3a3a3",
    "editorLineNumber.activeForeground": "#737373",
    "editor.selectionBackground": "#d4d4d4d9",
    "editor.inactiveSelectionBackground": "#d4d4d480",
  },
});

export { monaco };
