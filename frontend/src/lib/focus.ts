/**
 * The element focus should return to once a transient surface closes. When focus sits on a menu item
 * (which unmounts with its menu), this climbs to the trigger that owns the menu, through submenus.
 */
export function focusReturnTarget(): HTMLElement | null {
  let active: Element | null = document.activeElement;
  for (let depth = 0; active instanceof HTMLElement && depth < 4; depth += 1) {
    const menu = active.closest<HTMLElement>('[role="menu"]');
    if (!menu?.id) break;
    const trigger = [...document.querySelectorAll<HTMLElement>("[aria-controls]")]
      .find((element) => element.getAttribute("aria-controls") === menu.id);
    if (!trigger) break;
    active = trigger;
  }
  return active instanceof HTMLElement && active !== document.body && active !== document.documentElement
    ? active
    : null;
}
