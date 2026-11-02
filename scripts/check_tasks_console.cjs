const fs = require("node:fs");
const assert = require("node:assert/strict");
const { chromium } = require(process.env.RELAYFENCE_PLAYWRIGHT);
const url = process.env.RELAYFENCE_URL,
  token = process.env.RELAYFENCE_TOKEN;

async function api(operation, extra = {}) {
  const response = await fetch(url + "/v1/" + operation, {
    method: "POST",
    headers: {
      Authorization: "Bearer " + token,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ project: "tasks-ui", ...extra }),
  });
  return response.json();
}
async function mutate(operation, extra) {
  return api(operation, {
    actor: "browser-check",
    request_id: crypto.randomUUID(),
    ...extra,
  });
}

(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.RELAYFENCE_BROWSER,
    headless: true,
  });
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 1050 },
    });
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.goto(url);
    await page.locator("#project").fill("tasks-ui");
    await page.locator("#token").fill(token);
    await page.locator("#connect").click();
    await page.waitForFunction(() =>
      document.querySelector("#status").textContent.startsWith("Connected"),
    );
    async function create(id, title, dependencies = "", inputs = "") {
      await page.locator("#create-task").click();
      await page.locator("#assignment-id").fill(id);
      await page.locator("#assignment-title").fill(title);
      await page
        .locator("#assignment-description")
        .fill("Deliver working code and record the checks performed.");
      await page.locator("#assignment-priority").selectOption("2");
      await page.locator("#assignment-due").fill("2026-10-15T18:00");
      await page.locator("#assignment-dependencies").fill(dependencies);
      await page.locator("#assignment-inputs").fill(inputs);
      await page.locator("#save-assignment").click();
      await page.locator(`[data-task-id="${id}"]`).waitFor();
    }
    const row = (id) => page.locator(`[data-task-id="${id}"]`);
    async function action(id, name, expected) {
      await row(id).getByRole("button", { name, exact: true }).click();
      await page.waitForFunction(
        ({ id, expected }) =>
          document.querySelector(`[data-task-id="${id}"] .pill`)
            ?.textContent === expected,
        { id, expected },
      );
    }
    await create("design", "Design the shared API contract");
    await create(
      "build",
      "Implement the API and validate edge cases",
      "design",
      "api/spec",
    );
    assert.equal(await row("build").locator(".pill").textContent(), "blocked");
    await row("build").getByRole("button", { name: "Edit / reassign" }).click();
    await page.locator("#assignment-priority").selectOption("3");
    await page
      .locator("#assignment-description")
      .fill('<img src=x onerror="window.taskXss=true"> should render as text.');
    await page.locator("#save-assignment").click();
    await page.waitForFunction(() =>
      document
        .querySelector('[data-task-id="build"]')
        .textContent.includes("Urgent"),
    );
    await action("build", "Pause", "paused");
    await action("build", "Resume", "blocked");
    await action("build", "Cancel", "cancelled");
    await action("build", "Retry", "blocked");
    const claim = (await mutate("task_claim", { task_id: "design" })).data;
    assert.ok(claim);
    assert.equal(
      (
        await mutate("task_complete", {
          task_id: "design",
          fence: claim.task.fence,
          lease_token: claim.lease_token,
          outputs: [
            { key: "api/spec", expected_version: 0, value: { contract: "v1" } },
          ],
        })
      ).ok,
      true,
    );
    await page.locator("#refresh").click();
    await page.waitForFunction(
      () =>
        document.querySelector('[data-task-id="build"] .pill')?.textContent ===
        "ready",
    );
    const working = (await mutate("task_claim", { task_id: "build" })).data;
    await page.locator("#refresh").click();
    await page.waitForFunction(
      () =>
        document.querySelector('[data-task-id="build"] .pill')?.textContent ===
        "running",
    );
    await action("build", "Pause", "paused");
    const oldWrite = await mutate("task_complete", {
      task_id: "build",
      fence: working.task.fence,
      lease_token: working.lease_token,
      outputs: [{ key: "bad", expected_version: 0, value: "old" }],
    });
    assert.equal(oldWrite.error.code, "lease_lost");
    await action("build", "Resume", "ready");
    const retry = (await mutate("task_claim", { task_id: "build" })).data;
    await mutate("task_fail", {
      task_id: "build",
      fence: retry.task.fence,
      lease_token: retry.lease_token,
      error_code: "test_failure",
      message: "One acceptance check failed",
    });
    await page.locator("#refresh").click();
    await page.waitForFunction(
      () =>
        document.querySelector('[data-task-id="build"] .pill')?.textContent ===
        "failed",
    );
    await row("build")
      .getByRole("button", { name: "Details & history" })
      .click();
    await page.waitForFunction(() =>
      document
        .querySelector("#detail-history")
        .textContent.includes("task.failed"),
    );
    assert.match(
      await page.locator("#detail-content").textContent(),
      /should render as text/,
    );
    assert.equal(await page.evaluate(() => window.taskXss), undefined);
    assert.equal(await page.locator("#detail-content img").count(), 0);
    await page.locator("#close-detail").click();
    await page.locator("#task-search").fill("edge cases");
    await page.locator("#task-status").selectOption("failed");
    await page.locator("#task-filters button").click();
    await page.waitForFunction(
      () => document.querySelectorAll("#tasks .row").length === 1,
    );
    assert.equal(await row("design").count(), 0);
    await page.locator("#task-search").fill("");
    await page.locator("#task-status").selectOption("");
    await page.locator("#task-filters button").click();
    await page.waitForFunction(
      () => document.querySelectorAll("#tasks .row").length === 2,
    );
    await page.locator("#setup > summary").click();
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({
      path: "docs/assets/task-management.png",
      fullPage: true,
    });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      true,
    );
    await page.screenshot({
      path: ".relayfence/tasks-mobile.png",
      fullPage: true,
    });
    assert.deepEqual(errors, []);
    const report = {
      status: "passed",
      checks: [
        "create-edit-priority-due-date",
        "dependency-blocking",
        "pause-resume-cancel-retry",
        "live-lease-revocation",
        "failure-history",
        "server-filtering",
        "xss-safe-task-details",
        "mobile-layout",
        "no-js-errors",
      ],
    };
    fs.writeFileSync(
      ".relayfence/tasks-ui-report.json",
      JSON.stringify(report, null, 2),
    );
    console.log(JSON.stringify(report));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
