const fs = require("node:fs");
const assert = require("node:assert/strict");
const { chromium } = require(process.env.RELAYFENCE_PLAYWRIGHT);
const url = process.env.RELAYFENCE_URL,
  token = process.env.RELAYFENCE_TOKEN;

async function api(operation, body) {
  const response = await fetch(url + "/v1/" + operation, {
    method: "POST",
    headers: {
      Authorization: "Bearer " + token,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      project: "roles-ui",
      actor: "browser-check",
      request_id: crypto.randomUUID(),
      ...body,
    }),
  });
  const result = await response.json();
  assert.equal(result.ok, true, JSON.stringify(result));
  return result.data;
}

(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.RELAYFENCE_BROWSER,
    headless: true,
  });
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 1200 },
    });
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.goto(url);
    await page.locator("#project").fill("roles-ui");
    await page.locator("#token").fill("wrong");
    await page.locator("#connect").click();
    await page.waitForFunction(() =>
      document.querySelector("#status").textContent.includes("unauthorized"),
    );
    await page.locator("#token").fill(token);
    await page.locator("#connect").click();
    await page.waitForFunction(() =>
      document
        .querySelector("#status")
        .textContent.startsWith("Connected to roles-ui"),
    );
    await page.locator("#agents-editor > summary").click();
    await page.locator("#agent-id").fill("builder");
    await page.locator("#agent-label").fill("Build workspace");
    await page.locator("#agent-runtime").fill("custom");
    await page.locator("#agent-mode").selectOption("command");
    await page.locator("#agent-capabilities").fill("code, run-tests");
    await page.locator("#add-model").click();
    await page.locator("[data-model-id]").fill("demo-deep");
    await page.getByRole("button", { name: "Add supported parameter" }).click();
    await page.locator("[data-key]").fill("reasoning_effort");
    await page.locator("[data-values]").fill("low, high");
    await page.locator("#save-agent").click();
    await page.waitForFunction(
      () => document.querySelector("#role-agent").options.length === 1,
    );
    await page
      .locator("#agents button")
      .filter({ hasText: "Issue / rotate credential" })
      .click();
    await page.waitForFunction(
      () => document.querySelector("#issued-token").value.length >= 32,
    );
    const instanceToken = await page.locator("#issued-token").inputValue();
    assert.notEqual(instanceToken, token);
    await page.locator("#clear-credential").click();
    assert.equal(await page.locator("#issued-token").inputValue(), "");
    await page.locator("#new-agent").click();
    await page.locator("#agent-id").fill("reviewer");
    await page.locator("#agent-label").fill("External review session");
    await page.locator("#agent-runtime").fill("custom");
    await page.locator("#agent-capabilities").fill("code");
    await page.locator("#add-model").click();
    await page.locator("[data-model-id]").fill("demo-fast");
    await page.locator("#save-agent").click();
    await page.waitForFunction(
      () => document.querySelector("#role-agent").options.length === 2,
    );
    await page.locator("#agents-editor > summary").click();

    await page.locator("#role-id").fill("developer");
    await page.locator("#role-label").fill("Implementation");
    await page.locator("#role-agent").selectOption("builder");
    await page.locator("#role-model").selectOption("demo-deep");
    await page.locator("#role-parameters select").selectOption("high");
    await page
      .locator("#role-instructions")
      .fill("Implement the API contract and run the relevant tests.");
    await page
      .locator("#role-deliverables")
      .fill("Commit reference\nTest results");
    await page.locator("#role-capabilities").fill("code");
    await page.locator("#save-role").click();
    await page.waitForFunction(() =>
      document
        .querySelector("#roles")
        .textContent.includes("developer → builder → demo-deep"),
    );
    await page.locator("#assignment-id").fill("implement-api");
    await page.locator("#assignment-title").fill("Implement the API changes");
    await page.locator("#assignment-role").selectOption("developer");
    await page.locator("#save-assignment").click();
    await page.waitForFunction(() =>
      document
        .querySelector("#tasks")
        .textContent.includes("Implement the API changes"),
    );

    await page.locator("#role-agent").selectOption("reviewer");
    assert.equal(await page.locator("#role-parameters select").count(), 0);
    assert.equal(await page.locator("#role-model").inputValue(), "");
    assert.match(
      await page.locator("#model-hint").textContent(),
      /cannot switch/,
    );
    assert.equal(
      await page.locator('#role-model option[value="demo-deep"]').count(),
      0,
    );
    await page.locator("#role-model").selectOption("demo-fast");
    await page.locator("#save-role").click();
    await page.waitForFunction(() =>
      document
        .querySelector("#roles")
        .textContent.includes("developer → reviewer → demo-fast"),
    );
    await page.locator("#role-agent").selectOption("builder");
    await page.locator("#role-model").selectOption("demo-deep");
    await page.locator("#role-parameters select").selectOption("high");
    await page.locator("#save-role").click();
    await page.waitForFunction(() =>
      document
        .querySelector("#roles")
        .textContent.includes("developer → builder → demo-deep"),
    );
    await page.locator("#new-role").click();
    await page.locator("#role-id").fill("review");
    await page.locator("#role-label").fill("Code review");
    await page.locator("#role-agent").selectOption("reviewer");
    await page.locator("#role-model").selectOption("demo-fast");
    await page
      .locator("#role-instructions")
      .fill("Review correctness and identify missing coverage.");
    await page
      .locator("#role-deliverables")
      .fill("Review verdict\nFindings with file references");
    await page.locator("#save-role").click();
    await page.waitForFunction(
      () => document.querySelectorAll("#roles .binding").length === 2,
    );
    await page.locator("#tasks button").filter({ hasText: "Reassign" }).click();
    await page.locator("#assignment-role").selectOption("review");
    await page.locator("#save-assignment").click();
    await page.waitForFunction(() =>
      document.querySelector("#tasks").textContent.includes("Role: review"),
    );
    await page.locator("#new-assignment").click();
    await page.locator("#assignment-id").fill("review-api");
    await page
      .locator("#assignment-title")
      .fill("Review API contract and edge cases");
    await page.locator("#assignment-role").selectOption("review");
    await page.locator("#save-assignment").click();
    await page.waitForFunction(
      () => document.querySelectorAll("#tasks .row").length === 2,
    );
    await page
      .locator("#roles .binding")
      .filter({ hasText: "Implementation" })
      .getByRole("button", { name: "Edit", exact: true })
      .click();
    await page.evaluate(() => window.scrollTo(0, 0));
    assert.equal(await page.locator("#token").inputValue(), "");
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      true,
    );
    await page.screenshot({
      path: "docs/assets/roles-models.png",
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
      path: ".relayfence/roles-mobile.png",
      fullPage: true,
    });
    await api("agent_put", {
      agent_id: "builder",
      expected_revision: 2,
      label: '<img src=x onerror="window.__relayXss=true">',
      runtime: "custom",
      mode: "command",
      capabilities: ["code", "run-tests"],
      models: [{ id: "replacement-model" }],
    });
    await page.locator("#refresh").click();
    await page.waitForFunction(() =>
      document
        .querySelector("#role-model")
        .textContent.includes("no longer in catalogue"),
    );
    assert.equal(await page.locator("#role-model").inputValue(), "demo-deep");
    assert.equal(
      await page.locator("#role-parameters select").inputValue(),
      "high",
    );
    assert.match(
      await page.locator("#role-parameters").textContent(),
      /no longer supported/,
    );
    assert.equal(await page.locator("#agents img").count(), 0);
    assert.equal(await page.evaluate(() => Boolean(window.__relayXss)), false);
    await page.locator("#save-role").click();
    await page.waitForFunction(() =>
      document
        .querySelector("#configuration-status")
        .textContent.includes("unsupported_model"),
    );
    await page.locator("#project").fill("empty-project");
    await page.locator("#connect").click();
    await page.waitForFunction(() =>
      document
        .querySelector("#status")
        .textContent.startsWith("Connected to empty-project"),
    );
    assert.equal(await page.locator("#role-id").inputValue(), "");
    assert.equal(await page.locator("#roles .binding").count(), 0);
    assert.equal(await page.locator("#issued-token").inputValue(), "");
    assert.deepEqual(errors, []);
    const report = {
      status: "passed",
      checks: [
        "auth",
        "register-instance-and-model-options",
        "issue-and-clear-credential",
        "create-role-with-model-parameters",
        "dependent-selects-reset",
        "external-model-boundary",
        "save-role-edits",
        "create-and-reassign-task",
        "execution-ui-desktop",
        "mobile-no-overflow",
        "catalogue-changes-never-silently-fallback",
        "untrusted-registry-labels-render-as-text",
        "project-switch-clears-drafts",
        "no-js-errors",
      ],
    };
    fs.writeFileSync(
      ".relayfence/roles-ui-report.json",
      JSON.stringify(report, null, 2),
    );
    console.log(JSON.stringify(report));
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
