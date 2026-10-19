import { useEffect, useRef, type CSSProperties } from "react";
import { basicSetup } from "codemirror";
import { indentWithTab } from "@codemirror/commands";
import { HighlightStyle, LanguageDescription, syntaxHighlighting } from "@codemirror/language";
import { languages } from "@codemirror/language-data";
import { Compartment, EditorState, Prec, StateEffect } from "@codemirror/state";
import { EditorView, keymap, lineNumbers } from "@codemirror/view";
import { tags } from "@lezer/highlight";

interface EditableCodeEditorProps {
  path: string;
  value: string;
  theme: "light" | "dark";
  ariaLabel: string;
  startLine?: number;
  minLines?: number;
  maxLines?: number;
  autoFocus?: boolean;
  readOnly?: boolean;
  onChange(value: string): void;
  onCancel?(): void;
  onSubmit?(): void;
  onSubmitAndApply?(): void;
}

function editorTheme(theme: "light" | "dark") {
  const dark = theme === "dark";
  return [
    EditorView.theme({
      "&": { width: "100%", backgroundColor: "transparent", color: dark ? "#e5e5e5" : "#262626", fontSize: "13px" },
      ".cm-scroller": { overflow: "auto", fontFamily: 'ui-monospace, "SFMono-Regular", Menlo, Monaco, Consolas, "Liberation Mono", monospace', lineHeight: "20px" },
      ".cm-content": { padding: "10px 12px", caretColor: dark ? "#f5f5f5" : "#171717" },
      ".cm-line": { padding: "0" },
      ".cm-gutters": { backgroundColor: "transparent", color: dark ? "#a3a3a3" : "#525252", borderRight: "1px solid " + (dark ? "#404040" : "#d4d4d4") },
      ".cm-gutterElement": { padding: "0 8px" },
      ".cm-activeLine, .cm-activeLineGutter": { backgroundColor: dark ? "rgba(255,255,255,.04)" : "rgba(15,23,42,.04)" },
      ".cm-selectionBackground, &.cm-focused .cm-selectionBackground": { backgroundColor: dark ? "rgba(56,189,248,.22)" : "rgba(2,132,199,.16)" },
      "&.cm-focused": { outline: "none" },
    }, { dark }),
    syntaxHighlighting(HighlightStyle.define([
      { tag: tags.keyword, color: dark ? "#c4b5fd" : "#6d28d9" },
      { tag: [tags.string, tags.regexp], color: dark ? "#86efac" : "#166534" },
      { tag: [tags.number, tags.bool, tags.null], color: dark ? "#fdba74" : "#9a3412" },
      { tag: tags.comment, color: dark ? "#a3a3a3" : "#525252" },
      { tag: [tags.typeName, tags.className, tags.function(tags.variableName)], color: dark ? "#7dd3fc" : "#075985" },
    ])),
  ];
}

export default function EditableCodeEditor({
  path, value, theme, ariaLabel, startLine = 1, minLines = 6, maxLines = 18,
  autoFocus = false, readOnly = false, onChange, onCancel, onSubmit, onSubmitAndApply,
}: EditableCodeEditorProps) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<EditorView | null>(null);
  const themeConfig = useRef(new Compartment());
  const lineConfig = useRef(new Compartment());
  const editConfig = useRef(new Compartment());
  const callbacks = useRef({ onChange, onCancel, onSubmit, onSubmitAndApply, readOnly });
  callbacks.current = { onChange, onCancel, onSubmit, onSubmitAndApply, readOnly };
  const editorBounds = {
    "--review-code-editor-min-height": `${Math.max(1, minLines) * 20 + 20}px`,
    "--review-code-editor-max-height": `${Math.max(minLines, maxLines) * 20 + 20}px`,
    "--review-code-editor-content-height": `${Math.max(1, minLines) * 20}px`,
  } as CSSProperties;

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const view = new EditorView({
      parent: host,
      state: EditorState.create({
        doc: value,
        extensions: [
          basicSetup,
          themeConfig.current.of(editorTheme(theme)),
          lineConfig.current.of(lineNumbers({ formatNumber: (line) => String(startLine + line - 1) })),
          editConfig.current.of([EditorState.readOnly.of(readOnly), EditorView.editable.of(!readOnly)]),
          EditorView.contentAttributes.of({ "aria-label": ariaLabel, spellcheck: "false" }),
          EditorView.updateListener.of((update) => {
            if (update.docChanged) callbacks.current.onChange(update.state.doc.toString());
          }),
          Prec.highest(keymap.of([
            { key: "Mod-Shift-Enter", run: () => {
              if (!callbacks.current.onSubmitAndApply) return false;
              if (!callbacks.current.readOnly) callbacks.current.onSubmitAndApply();
              return true;
            } },
            { key: "Mod-Enter", run: () => {
              if (!callbacks.current.onSubmit) return false;
              if (!callbacks.current.readOnly) callbacks.current.onSubmit();
              return true;
            } },
            { key: "Escape", run: () => {
              if (!callbacks.current.onCancel) return false;
              callbacks.current.onCancel();
              return true;
            } },
          ])),
          keymap.of([indentWithTab]),
        ],
      }),
    });
    viewRef.current = view;
    const language = LanguageDescription.matchFilename(languages, path);
    if (language) {
      void language.load().then((support) => {
        if (view.dom.isConnected) view.dispatch({ effects: StateEffect.appendConfig.of(support) });
      }).catch(() => undefined);
    }
    const focusFrame = autoFocus ? window.requestAnimationFrame(() => {
      if (view.dom.isConnected) view.focus();
    }) : null;
    return () => {
      if (focusFrame !== null) window.cancelAnimationFrame(focusFrame);
      viewRef.current = null;
      view.destroy();
    };
    // Reconfigure presentation below without losing the cursor or undo history.
  }, [ariaLabel, autoFocus, path]);

  useEffect(() => {
    viewRef.current?.dispatch({ effects: [
      themeConfig.current.reconfigure(editorTheme(theme)),
      lineConfig.current.reconfigure(lineNumbers({ formatNumber: (line) => String(startLine + line - 1) })),
      editConfig.current.reconfigure([EditorState.readOnly.of(readOnly), EditorView.editable.of(!readOnly)]),
    ] });
  }, [theme, startLine, readOnly]);

  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    const current = view.state.doc.toString();
    if (current !== value) view.dispatch({ changes: { from: 0, to: current.length, insert: value } });
  }, [value]);

  return <div ref={hostRef} className="review-code-editor focus-within:border-ring" data-testid="review-code-editor" style={editorBounds} />;
}
