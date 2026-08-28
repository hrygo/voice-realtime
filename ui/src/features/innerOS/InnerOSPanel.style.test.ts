import { describe, expect, it } from "vitest";

// The browser bundle does not include this file; Vitest executes it in Node.
// @ts-expect-error Vitest-only source inspection uses Node's filesystem API.
import { readFileSync } from "node:fs";

const panelCss = readFileSync("src/features/innerOS/InnerOSPanel.css", "utf8");
const tokensCss = readFileSync("src/features/innerOS/InnerOSTokens.css", "utf8");
const answerCss = readFileSync("src/features/innerOS/InnerOSAnswerCard.css", "utf8");
const archiveCss = readFileSync("src/features/innerOS/InnerOSArchive.css", "utf8");

function getRuleBody(selector: string): string {
  const escapedSelector = selector.replace(/[.*+?^${}()|[\\]\\]/g, "\\$&");
  const match = panelCss.match(new RegExp(`${escapedSelector}\\s*\\{([^}]*)\\}`));
  expect(match, `Missing CSS rule for ${selector}`).not.toBeNull();
  return match?.[1] ?? "";
}

describe("Inner OS visual contract", () => {
  it("scopes feature tokens instead of publishing a second root palette", () => {
    expect(tokensCss).toMatch(/\.inner-os-panel,\s*\.inner-os-unsaved-tray,\s*\.inner-os-history-tab\s*\{/);
    expect(tokensCss).not.toMatch(/^:root\s*\{/m);
    expect(`${panelCss}${tokensCss}${answerCss}${archiveCss}`).not.toMatch(/#[0-9a-f]{3,8}/i);
  });

  it("keeps the composer input and send action on one grid baseline", () => {
    const inputRule = getRuleBody(".inner-os-dock-input-wrap");
    const submitRule = getRuleBody(".inner-os-submit-btn");

    expect(inputRule).toMatch(/display:\s*grid/);
    expect(inputRule).toMatch(/grid-template-columns:\s*minmax\(0, 1fr\) 48px/);
    expect(submitRule).toMatch(/width:\s*48px/);
    expect(submitRule).toMatch(/height:\s*100%/);
    expect(submitRule).toMatch(/align-self:\s*stretch/);
  });
});
