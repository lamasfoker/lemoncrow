import { createContext, useContext } from "react";

/**
 * Where overlays (dialogs, sheets, menus, tooltips, select popups) portal to. The app shell
 * provides its root element so portaled content keeps the shell's scoped type, focus and
 * theme rules; without a provider (isolated tests) overlays fall back to document.body.
 */
export const PortalContainerContext = createContext<HTMLElement | null>(null);

export function usePortalContainer(): HTMLElement | undefined {
  return useContext(PortalContainerContext) ?? undefined;
}
