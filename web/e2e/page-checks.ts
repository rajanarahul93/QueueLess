import AxeBuilder from "@axe-core/playwright";
import { expect, type Page } from "@playwright/test";

export async function noAxeViolations(page: Page, screen: string) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  expect(results.violations.map((violation) => `${screen}: ${violation.id}`)).toEqual([]);
}

/** Init script: counts the moments the loaded page had no title (a remounted <title> leaves the
 *  tab untitled between two DOM updates). Add it to a context before its pages load. */
export function countUntitled() {
  addEventListener("DOMContentLoaded", () => {
    const counter = window as unknown as { untitled: number };
    counter.untitled = 0;
    new MutationObserver(() => {
      if (!document.title) counter.untitled += 1;
    }).observe(document.head, { subtree: true, childList: true, characterData: true });
  });
}

/** The page has this title now and never lost its title since it loaded. */
export async function expectTitle(page: Page, title: string) {
  await expect(page).toHaveTitle(title);
  expect(await page.evaluate(() => (window as unknown as { untitled: number }).untitled)).toBe(0);
}
