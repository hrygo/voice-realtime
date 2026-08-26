import { describe, expect, it } from "vitest";

// The browser bundle does not include this file; Vitest executes it in Node.
// The UI project intentionally does not carry the Node type package.
// @ts-expect-error Vitest-only source inspection uses Node's filesystem API.
import { readFileSync } from "node:fs";

const meetingPanelCss = readFileSync("src/components/meeting/MeetingPanel.css", "utf8");

function getRuleBody(selector: string): string {
  const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = meetingPanelCss.match(new RegExp(`${escapedSelector}\\s*\\{([^}]*)\\}`));
  expect(match, `Missing CSS rule for ${selector}`).not.toBeNull();
  return match?.[1] ?? "";
}

function getNumericDeclaration(ruleBody: string, property: string): number {
  const match = ruleBody.match(new RegExp(`${property}\\s*:\\s*(\\d+)`));
  expect(match, `Missing numeric ${property} declaration`).not.toBeNull();
  return Number(match?.[1]);
}

describe("MeetingPanel stacking layers", () => {
  it("keeps the detail navigation above the dual-pane controls", () => {
    const navigationRule = getRuleBody(".detail-top-nav-bar");
    const splitterRule = getRuleBody(".pane-splitter");

    expect(navigationRule).toMatch(/position\s*:\s*relative/);
    expect(getNumericDeclaration(navigationRule, "z-index")).toBeGreaterThan(
      getNumericDeclaration(splitterRule, "z-index"),
    );
  });
});
