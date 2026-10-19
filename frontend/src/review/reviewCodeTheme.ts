import { applyTheme, getInitialTheme, type Theme } from "../lib/theme";

export type ReviewCodeTheme = "lemoncrow" | "lemoncrow-dark" | "github-dark" | "github-light";

export interface ReviewCodeThemeDefinition {
  id: ReviewCodeTheme;
  label: string;
  pierreTheme: string;
  chromeTheme: Theme;
}

const STORAGE_KEY = "lemoncrow-review-code-theme";

export const REVIEW_CODE_THEMES: readonly ReviewCodeThemeDefinition[] = [
  { id: "lemoncrow", label: "LemonCrow", pierreTheme: "pierre-light", chromeTheme: "light" },
  { id: "lemoncrow-dark", label: "LemonCrow Dark", pierreTheme: "pierre-dark", chromeTheme: "dark" },
  { id: "github-dark", label: "GitHub Dark", pierreTheme: "github-dark-default", chromeTheme: "dark" },
  { id: "github-light", label: "GitHub Light", pierreTheme: "github-light-default", chromeTheme: "light" },
];

export function reviewCodeThemeDefinition(id: ReviewCodeTheme): ReviewCodeThemeDefinition {
  return REVIEW_CODE_THEMES.find((item) => item.id === id) ?? REVIEW_CODE_THEMES[0];
}

export function getInitialReviewCodeTheme(): ReviewCodeTheme {
  const chromeTheme = getInitialTheme();
  let saved: ReviewCodeTheme | null = null;
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    if (REVIEW_CODE_THEMES.some((item) => item.id === value)) saved = value as ReviewCodeTheme;
  } catch {
    /* localStorage unavailable */
  }
  if (saved?.startsWith("github")) return chromeTheme === "dark" ? "github-dark" : "github-light";
  return chromeTheme === "dark" ? "lemoncrow-dark" : "lemoncrow";
}

export function applyReviewCodeTheme(id: ReviewCodeTheme): Theme {
  const definition = reviewCodeThemeDefinition(id);
  applyTheme(definition.chromeTheme);
  try {
    localStorage.setItem(STORAGE_KEY, id);
  } catch {
    /* localStorage unavailable */
  }
  return definition.chromeTheme;
}
