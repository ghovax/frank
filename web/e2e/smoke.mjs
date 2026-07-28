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
// `/__frank/runtime.json` is a deliberate probe for "am I being served by `frank web`?".
// Under `next dev` nothing answers it and the code handles that; the browser still logs the
// 404, so filter it out rather than let a healthy run look broken.
const EXPECTED_NOISE = /__frank\/runtime\.json/;
page.on("console", (message) => {
  if (message.type() !== "error") return;
  // A failed-resource log carries the URL on the location, not in the text.
  const where = message.location()?.url ?? "";
  if (EXPECTED_NOISE.test(where) || EXPECTED_NOISE.test(message.text())) return;
  consoleErrors.push(`${message.text()} ${where}`.trim());
});
page.on("pageerror", (error) => consoleErrors.push(String(error)));

try {
  // Not `networkidle`: with the connection gate gone the app opens a live attach stream as
  // soon as it mounts, so the network is never idle and waiting for it always times out.
  await page.goto(APP, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(3500);  // React has to hydrate before a click has a handler.

  // The composer is the signal that the app has mounted. There is no gate to clear: the
  // interface talks to the local daemon, and reaching elsewhere is a property of the folder.
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
  // reload. That is the bug this file exists for: the live frames were arriving and being
  // handed to a reducer that walked `.parts` on something that was a single part, so every
  // one of them reduced to nothing and answers only showed up on a refresh.
  //
  // Three in a row, not one: the first message also creates the session, so a fault in the
  // per-turn attach lifecycle hides behind a passing first round.
  for (const round of [1, 2, 3]) {
    const marker = `PLAYWRIGHT-${round}-${Date.now() % 100000}`;
    await composer.fill(`Reply with exactly ${marker} and nothing else.`);
    await composer.press("Enter");

    let appeared = false;
    for (let waited = 0; waited < 120000; waited += 1000) {
      await page.waitForTimeout(1000);
      const text = await page.locator("body").innerText();
      // Twice: once in the prompt we typed, once in the answer.
      if (text.split(marker).length > 2) {
        appeared = true;
        break;
      }
    }
    check(`reply ${round} appears live, with no reload`, appeared);
    if (!appeared) break;

    // The turn must also *end*. It ends when the session says so on the stream, not when
    // the stream closes — the stream is the session's and stays open across turns. When
    // that was confused, the answer arrived and the turn never finished: Stop stayed up
    // and the next message was routed as steering into a turn already over.
    const stopped = await page
      .getByRole("button", { name: "Stop" })
      .waitFor({ state: "hidden", timeout: 60000 })
      .then(() => true)
      .catch(() => false);
    check(`turn ${round} ends: the Stop button goes away`, stopped);
    if (!stopped) break;

    const composerPlaceholder = await composer.getAttribute("placeholder");
    check(
      `composer ${round} returns to normal, not queueing`,
      !/queue/i.test(composerPlaceholder ?? ""),
      composerPlaceholder ?? "",
    );
  }

  // A message sent after a turn has ended starts its own turn; it is not steering.
  const steeringChips = await page.getByText(/steering next opening/i).count();
  check("no message was mistaken for steering", steeringChips === 0, `${steeringChips} chips`);

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
