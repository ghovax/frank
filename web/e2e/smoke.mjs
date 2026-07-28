/**
 * A headless drive of the real interface, against a real daemon.
 *
 * Written because every failure in this area was invisible from the outside: the server
 * answered every call correctly while the browser showed nothing, and checking the API told
 * us the turn had been *accepted*, which is not the same as an answer appearing on screen.
 * This asserts what a person would look at.
 *
 * Uses the system Chrome rather than a downloaded browser, so nothing is fetched to run it.
 *
 *   cd web && bun e2e/smoke.mjs
 */

import { chromium } from "playwright";

const APP = process.env.FRANK_APP_URL ?? "http://localhost:3000";
const results = [];

function check(label, passed, detail = "") {
  results.push({ label, passed, detail });
  console.log(`  ${passed ? "PASS" : "FAIL"}  ${label}${detail ? `  [${detail}]` : ""}`);
}

const browser = await chromium.launch({ channel: "chrome", headless: true });
const page = await browser.newPage({ viewport: { width: 1400, height: 950 } });

const consoleErrors = [];
page.on("console", (message) => {
  if (message.type() === "error") consoleErrors.push(message.text());
});
page.on("pageerror", (error) => consoleErrors.push(String(error)));

try {
  await page.goto(APP, { waitUntil: "networkidle" });
  await page.waitForTimeout(2500);  // React has to hydrate before a click has a handler.

  // The connection gate: connect to the local daemon if it is offering. Exact name, because
  // "Save connection" also matches a loose /connect/i and clicking that does nothing here.
  const connect = page.getByRole("button", { name: "Connect", exact: true }).first();
  if (await connect.isVisible({ timeout: 20000 }).catch(() => false)) {
    await connect.click();
    // The gate holds the attempt for a beat deliberately, so give it room to resolve.
    await page.waitForTimeout(3000);
  }

  // The composer is the signal that we are past the gate and into the app.
  const composer = page.locator("textarea").first();
  await composer.waitFor({ state: "visible", timeout: 45000 });
  check("interface reaches the chat view", true);

  // An agent must be selected, or a send silently does nothing.
  const bodyText = await page.locator("body").innerText();
  check(
    "an agent is selected by default",
    /Code implementer|General assistant|Code investigator|Senior researcher/i.test(bodyText),
    (bodyText.match(/(Code implementer|General assistant|Code investigator|Senior researcher)/i) ?? [])[0] ?? "none",
  );

  // The turn itself, and — the part that matters — whether the answer appears *without* a
  // reload. That is the bug this file exists for.
  const marker = `PLAYWRIGHT-${Date.now() % 100000}`;
  await composer.fill(`Reply with exactly ${marker} and nothing else.`);
  await composer.press("Enter");

  let appeared = false;
  for (let waited = 0; waited < 240000; waited += 2000) {
    await page.waitForTimeout(2000);
    const text = await page.locator("body").innerText();
    if (text.includes(marker) && text.split(marker).length > 2) {
      appeared = true;
      break;
    }
  }
  check("the reply appears live, with no reload", appeared);

  // Hovering one conversation must not act on the others. Regression guard for the project
  // row's descendant selectors matching every nested row.
  const rows = page.locator(".sidebar-row");
  const rowCount = await rows.count();
  if (rowCount >= 2) {
    await rows.nth(rowCount - 1).hover();
    await page.waitForTimeout(400);
    const revealed = await page.evaluate(() =>
      Array.from(document.querySelectorAll("[data-row-actions]")).filter(
        (node) => Number(getComputedStyle(node).opacity) > 0.5,
      ).length,
    );
    check("hovering one row reveals only that row's actions", revealed <= 1, `${revealed} revealed`);
  } else {
    check("hovering one row reveals only that row's actions", true, "skipped: too few rows");
  }

  check("no uncaught console errors", consoleErrors.length === 0, consoleErrors.slice(0, 2).join(" | "));
} catch (error) {
  check("run completed without throwing", false, String(error).slice(0, 160));
  console.log("\n  --- page console ---");
  for (const line of consoleErrors.slice(0, 12)) console.log("   ", line.slice(0, 220));
  console.log("  --- visible text ---");
  const seen = await page.locator("body").innerText().catch(() => "");
  console.log("   ", seen.replace(/\n+/g, " | ").slice(0, 400));
} finally {
  await page.screenshot({ path: "/tmp/frank-e2e.png", fullPage: false }).catch(() => {});
  await browser.close();
}

const failed = results.filter((entry) => !entry.passed).length;
console.log(`\n  ${results.length - failed}/${results.length} passed  (screenshot: /tmp/frank-e2e.png)`);
process.exit(failed === 0 ? 0 : 1);
