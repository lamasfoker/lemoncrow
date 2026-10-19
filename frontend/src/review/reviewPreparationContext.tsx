import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

export interface PreparationStep {
  key: string;
  label: string;
  state: "pending" | "running" | "done" | "skipped" | "failed";
  elapsed: number | null;
  started_at?: number;
}

export interface ReaderPreparation {
  pending: boolean;
  steps: PreparationStep[];
  onOpenCode?: () => void;
}

const PreparationContext = createContext<{
  reader: ReaderPreparation | null;
  setReader: (value: ReaderPreparation | null) => void;
} | null>(null);

export function ReviewPreparationProvider({ children }: { children: ReactNode }) {
  const [reader, setReader] = useState<ReaderPreparation | null>(null);
  const value = useMemo(() => ({ reader, setReader }), [reader]);
  return <PreparationContext.Provider value={value}>{children}</PreparationContext.Provider>;
}

export function useReviewPreparation() {
  return useContext(PreparationContext);
}
