import { describe, expect, it } from "vitest";

// The browser bundle does not include this file; Vitest executes it in Node.
// @ts-expect-error Vitest-only source inspection uses Node's filesystem API.
import { readFileSync } from "node:fs";

const modeSidebarCss = readFileSync("src/components/ModeSidebar.css", "utf8");

function getRuleBody(selector: string): string {
  const escapedSelector = selector.replace(/[.*+?^${}()|[\\]\\]/g, "\\$&");
  const match = modeSidebarCss.match(new RegExp(`${escapedSelector}\\s*\\{([^}]*)\\}`));
  expect(match, `Missing CSS rule for ${selector}`).not.toBeNull();
  return match?.[1] ?? "";
}

describe("collapsed meeting sidebar rail", () => {
  it("uses the full rail width so action buttons are centered", () => {
    const rule = getRuleBody(".mode-sidebar.is-collapsed .meeting-sidebar-collapsed-strip");

    expect(rule).toMatch(/position\s*:\s*absolute/);
    expect(rule).toMatch(/inset\s*:\s*0/);
    expect(rule).toMatch(/width\s*:\s*auto/);
    expect(rule).toMatch(/padding\s*:\s*12px 0 0/);
    expect(rule).toMatch(/box-sizing\s*:\s*border-box/);
  });
});
