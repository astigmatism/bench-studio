import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
const profiles = JSON.parse(
  readFileSync(new URL("./profiles.json", import.meta.url), "utf8"),
);
const profile = profiles.find((p: any) => p.id === "coding-sessions");
const base = {
  id: "session-ui",
  profile: "coding-sessions",
  profile_spec: {
    ...profile,
    difficulty: "small",
    task_selection: "all",
    repetitions: 1,
    review_mode: "interactive",
  },
  family: "session",
  status: "awaiting_review",
  mode: "sequential",
  created_at: "2026-09-18T12:00:00Z",
  started_at: "2026-09-18T12:00:01Z",
  requested_targets: ["daytime"],
  resolved: { daytime: { canonical: "Local model", context: 131072 } },
  summary: {
    daytime: {
      count: 2,
      passed: 0,
      score: null,
      tasks: [],
      session_metrics: [],
    },
  },
  reviews: [
    {
      run_id: "session-ui",
      target: "daytime",
      attempt_id: "issues-small-r1",
      revision: 1,
      kind: "plan",
      artifact: "daytime/issues-small-r1/plan-1.json",
      decision: null,
    },
  ],
  session_progress: { phase: "review_wait", attempt_id: "issues-small-r1" },
  artifacts: [],
};
async function setup(page: any, run: any = base, setupState: any = null) {
  await page.route("**/api/**", async (route: any) => {
    const path = new URL(route.request().url()).pathname;
    let body: any = {};
    if (path === "/api/health")
      body = {
        ok: true,
        revision: "test",
        runner: {},
        session_setup: setupState,
      };
    else if (path === "/api/session-setup/start") {
      setupState = {
        phase: "requested",
        detail: "Setup requested. Waiting for the controller.",
        can_start: false,
        can_stop: true,
      };
      body = { active: true };
    } else if (path === "/api/session-setup/stop") {
      setupState = {
        phase: "paused",
        detail:
          "Suite setup is idle. Updates and restarts do not start checks.",
        can_start: true,
        can_stop: false,
      };
      body = { active: false };
    } else if (path === "/api/models")
      body = {
        runtime: { ready: true },
        models: [
          {
            alias: "daytime",
            canonical: "Local model",
            context: 131072,
            available: true,
            vision: true,
            reasoning: { efforts: { off: "none" } },
            gpus: ["GPU"],
          },
        ],
      };
    else if (path === "/api/profiles")
      body = profiles.map((p: any) =>
        run.profile_spec.qualification && p.id === run.profile
          ? {
              ...p,
              preparation: {
                ready: false,
                prepared: true,
                reason: "Live qualification pending.",
              },
            }
          : p,
      );
    else if (path === "/api/runs")
      body =
        route.request().method() === "POST"
          ? { ...run, status: "queued" }
          : [run];
    else if (path === "/api/events")
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: "retry: 60000\n\n",
      });
    else if (path.endsWith("plan-1.json"))
      body = {
        summary: "Add issue filters",
        steps: ["Inspect list", "Add combined filters"],
        checks: ["Reset empty state", "Regression checks"],
      };
    else if (path.includes("/reviews/") && route.request().method() === "POST")
      body = { decision: route.request().postDataJSON().action };
    else if (path.includes("/artifacts/") && path.endsWith(".html"))
      return route.fulfill({
        status: 200,
        contentType: "text/html",
        body: "<button>Clickable prototype</button>",
      });
    else if (path.startsWith("/api/runs/")) body = run;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });
  await page.goto("/");
  if (setupState) await openSetup(page);
}
async function openSetup(page: any) {
  await page.getByRole("button", { name: "Profiles", exact: true }).click();
  await page.locator("summary").filter({ hasText: "Suite setup" }).click();
}

test("setup only starts on click and stop persists across browser reloads", async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const mutations: string[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST") mutations.push(new URL(r.url()).pathname);
  });
  await setup(page, base, {
    phase: "paused",
    detail:
      "Suite setup is idle. Select Start setup to validate fixtures and run model smoke tests. Updates and restarts do not start checks.",
    can_start: true,
    can_stop: false,
  });
  await expect(
    page.getByRole("button", { name: "Start setup", exact: true }),
  ).toBeVisible();
  expect(await page.evaluate(() => window.isSecureContext)).toBe(
    testInfo.project.name !== "lan-http",
  );
  await page.reload();
  await openSetup(page);
  await expect(
    page.getByRole("button", { name: "Start setup", exact: true }),
  ).toBeVisible();
  expect(mutations).toEqual([]);
  const start = page.waitForRequest(
    (r) => r.url().endsWith("/session-setup/start") && r.method() === "POST",
  );
  await page.getByRole("button", { name: "Start setup", exact: true }).click();
  const preparationRequest = (await start).postDataJSON();
  expect(preparationRequest.idempotency_key).toBeTruthy();
  expect(preparationRequest.run_smoke).toBe(false);
  await expect(
    page.getByText("Setup request accepted.", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Stop setup", exact: true }),
  ).toBeVisible();
  await page.reload();
  await openSetup(page);
  await expect(
    page.getByRole("button", { name: "Stop setup", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Stop setup", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Start setup", exact: true }),
  ).toBeVisible();
  await page.reload();
  await openSetup(page);
  await expect(
    page.getByRole("button", { name: "Start setup", exact: true }),
  ).toBeVisible();
  expect(mutations).toEqual([
    "/api/session-setup/start",
    "/api/session-setup/stop",
  ]);
  expect(errors).toEqual([]);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/setup-manual-mobile.png",
    fullPage: true,
  });
});

test("model smoke tests require an explicit choice", async ({ page }) => {
  await setup(page, base, {
    phase: "paused",
    detail: "Prepare benchmark fixtures.",
    can_start: true,
  });
  const option = page.getByLabel(
    "Also run model smoke tests (optional; uses the model)",
  );
  await expect(option).not.toBeChecked();
  await option.check();
  const request = page.waitForRequest(
    (r) => r.url().endsWith("/session-setup/start") && r.method() === "POST",
  );
  await page.getByRole("button", { name: "Start setup", exact: true }).click();
  expect((await request).postDataJSON().run_smoke).toBe(true);
});

test("failed model smoke leaves prepared profiles launchable after reload", async ({
  page,
}) => {
  await setup(page, base, {
    phase: "ready",
    detail:
      "Fixtures validated. All benchmark suites are available. Some model smoke tests did not pass.",
    can_start: true,
    can_stop: false,
    suites: {
      "coding-sessions": {
        available: true,
        phase: "failed",
        detail: "No usable answer after three generation attempts",
        run_id: base.id,
      },
    },
  });
  await page.reload();
  await openSetup(page);
  const banner = page.getByRole("status", { name: "Benchmark suite setup" });
  await expect(banner).toContainText("Coding sessions: Available");
  await expect(banner).toContainText("Model smoke test: Failed");
  await expect(
    banner.getByRole("button", { name: "Run optional model checks" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "New benchmark", exact: true })
    .click();
  await page
    .getByLabel("Profile", { exact: true })
    .selectOption("coding-sessions");
  await expect(
    page.getByRole("button", { name: "Queue benchmark", exact: true }),
  ).toBeEnabled();
  const request = page.waitForRequest(
    (r) => r.url().endsWith("/api/runs") && r.method() === "POST",
  );
  await page
    .getByRole("button", { name: "Queue benchmark", exact: true })
    .click();
  expect((await request).postDataJSON().qualification).not.toBe(true);
});

test("setup shows pending feedback and server failures", async ({ page }) => {
  await setup(page, base, {
    phase: "paused",
    detail: "Suite setup is idle.",
    can_start: true,
    can_stop: false,
  });
  let respond!: () => void;
  const pending = new Promise<void>((resolve) => {
    respond = resolve;
  });
  await page.route("**/api/session-setup/start", async (route) => {
    await pending;
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Application update in progress" }),
    });
  });
  await page.getByRole("button", { name: "Start setup", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Starting setup…", exact: true }),
  ).toBeDisabled();
  respond();
  await expect(page.getByRole("alert")).toContainText(
    "Could not start suite setup: Error: Application update in progress",
  );
  await expect(
    page.getByRole("button", { name: "Start setup", exact: true }),
  ).toBeEnabled();
});

test("setup reports browser-side failures instead of silently ignoring the click", async ({
  page,
}) => {
  await page.addInitScript(() => {
    Object.defineProperty(crypto, "getRandomValues", {
      value: () => {
        throw Error("Browser random source unavailable");
      },
    });
  });
  await setup(page, base, {
    phase: "paused",
    detail: "Suite setup is idle.",
    can_start: true,
    can_stop: false,
  });
  await page.getByRole("button", { name: "Start setup", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText(
    "Browser random source unavailable",
  );
  await expect(
    page.getByRole("button", { name: "Start setup", exact: true }),
  ).toBeEnabled();
});

test("launch session tiers, selection and repetitions", async ({ page }) => {
  await setup(page);
  await page
    .getByRole("button", { name: "New benchmark", exact: true })
    .click();
  await page
    .getByLabel("Profile", { exact: true })
    .selectOption("coding-sessions");
  await expect(page.getByLabel("Review mode")).toHaveValue("interactive");
  await page.getByLabel("Task difficulty").selectOption("large");
  await page.getByLabel("Task selection").selectOption("inventory-large");
  await page.getByLabel("Repetitions").selectOption("3");
  await page.getByLabel("Review mode").selectOption("unattended");
  await expect(
    page.getByText("3 independent attempts per model", { exact: false }),
  ).toBeVisible();
  const request = page.waitForRequest(
    (r) => r.url().endsWith("/api/runs") && r.method() === "POST",
  );
  await page
    .getByRole("button", { name: "Queue benchmark", exact: true })
    .click();
  const payload = (await request).postDataJSON();
  expect(payload).toMatchObject({
    difficulty: "large",
    task_selection: "inventory-large",
    repetitions: 3,
    review_mode: "unattended",
    mode: "sequential",
  });
  expect(payload.overrides.task_timeout).toBe(14400);
});
test("approve plan without replaying after a double click", async ({
  page,
}) => {
  await setup(page);
  await page.getByRole("button", { name: "View run", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Review the plan" }),
  ).toBeVisible();
  await expect(
    page.getByText("Add combined filters", { exact: true }),
  ).toBeVisible();
  const request = page.waitForRequest(
    (r) => r.url().includes("/reviews/") && r.method() === "POST",
  );
  await page.getByRole("button", { name: "Approve plan", exact: true }).click();
  expect((await request).postDataJSON().action).toBe("approve");
  await expect(page.getByRole("status")).toContainText("Decision recorded");
  await expect(
    page.getByRole("button", { name: "Approve plan", exact: true }),
  ).toHaveCount(0);
});
test("request revision with feedback and show a sandboxed prototype", async ({
  page,
}) => {
  const run = structuredClone(base);
  run.reviews[0] = {
    ...run.reviews[0],
    kind: "prototype",
    artifact: "daytime/issues-small-r1/verification-1/prototype.html",
  };
  await setup(page, run);
  await page.getByRole("button", { name: "View run", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Request revision", exact: true }),
  ).toBeDisabled();
  await page
    .getByLabel("Revision feedback")
    .fill("Make the filters usable on mobile.");
  const request = page.waitForRequest(
    (r) => r.url().includes("/reviews/") && r.method() === "POST",
  );
  await page
    .getByRole("button", { name: "Request revision", exact: true })
    .click();
  expect((await request).postDataJSON()).toMatchObject({
    action: "revise",
    feedback: "Make the filters usable on mobile.",
  });
  await expect(
    page.locator('iframe[title="Interactive prototype"]'),
  ).toHaveAttribute("sandbox", "allow-scripts");
  await expect(
    page
      .frameLocator('iframe[title="Interactive prototype"]')
      .getByRole("button", { name: "Clickable prototype" }),
  ).toBeVisible();
});
test("vision suite has fixed tasks and no difficulty selector", async ({
  page,
}) => {
  await setup(page);
  await page
    .getByRole("button", { name: "New benchmark", exact: true })
    .click();
  await page
    .getByLabel("Profile", { exact: true })
    .selectOption("vision-checks");
  await expect(page.getByLabel("Task difficulty")).toHaveCount(0);
  await expect(
    page.getByText("12 independent attempts per model", { exact: false }),
  ).toBeVisible();
  await page
    .getByLabel("Profile", { exact: true })
    .selectOption("visual-design");
  await expect(page.getByLabel("Task difficulty")).toHaveValue("small");
  await expect(page.getByLabel("Review mode")).toHaveValue("interactive");
});

test("completed sessions show success and consumed time together", async ({
  page,
}) => {
  const run: any = structuredClone(base);
  run.status = "completed";
  run.reviews = [];
  run.summary.daytime = {
    count: 2,
    passed: 1,
    score: 50,
    unit: "%",
    metric: "commit_ready_rate",
    session_metrics: [
      {
        name: "commit_ready_rate",
        label: "Commit-ready rate",
        value: 50,
        unit: "%",
        direction: "higher",
      },
      {
        name: "implementation_seconds",
        label: "Median implementation time · successful attempts",
        value: 120,
        unit: "s",
        direction: "lower",
      },
      {
        name: "consumed_seconds",
        label: "Active time · all attempts",
        value: 1940,
        unit: "s",
        direction: "lower",
      },
    ],
    tasks: [
      {
        id: "issues-small-r1",
        status: "passed",
        active_seconds: 140,
        implementation_seconds: 120,
      },
      {
        id: "inventory-small-r1",
        status: "failed",
        active_seconds: 1800,
        detail: "Active-time limit exhausted",
        failure_kind: "agent_budget",
      },
    ],
  };
  await setup(page, run);
  await page
    .getByRole("button", { name: "Coding sessions", exact: true })
    .click();
  await expect(
    page.getByText("Median implementation time · successful attempts", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByText("Active time · all attempts", { exact: true }),
  ).toBeVisible();
  await page
    .getByText("inventory-small-r1 · failed · 1,800s active", { exact: true })
    .click();
  await expect(
    page.getByText("Outcome: agent budget", { exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: "test-results/session-results-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/session-results-mobile.png",
    fullPage: true,
  });
});

test("profile setup preserves details and failed qualification can be retried", async ({
  page,
}) => {
  const run: any = structuredClone(base);
  run.status = "completed";
  run.reviews = [];
  run.profile_spec = {
    ...run.profile_spec,
    qualification: true,
    task_selection: "issues-small",
    review_mode: "unattended",
  };
  await setup(page, run, {
    phase: "needs_attention",
    detail: "Offline preparation passed. Some smoke runs need attention.",
    suites: {
      "coding-sessions": {
        phase: "not_passed",
        run_id: run.id,
        detail:
          "Smoke run finished without passing qualification. Open the run for test evidence, then use Run again to retry.",
      },
      "vision-checks": {
        phase: "waiting_for_model",
        detail:
          "Waiting for an available daytime model with advertised vision support.",
      },
    },
  });
  await expect(page.getByRole("status")).toContainText(
    "Coding sessions: Not passed",
  );
  await expect(page.getByRole("status")).toContainText(
    "Waiting for an available daytime model with advertised vision support.",
  );
  await page
    .getByRole("button", { name: "View Coding sessions smoke run" })
    .click();
  await page.getByRole("button", { name: "Run again", exact: true }).click();
  await expect(page.getByLabel("Task selection")).toHaveValue("issues-small");
  await expect(page.getByLabel("Review mode")).toHaveValue("unattended");
  const request = page.waitForRequest(
    (r) => r.url().endsWith("/api/runs") && r.method() === "POST",
  );
  await page
    .getByRole("button", { name: "Queue benchmark", exact: true })
    .click();
  expect((await request).postDataJSON()).toMatchObject({
    qualification: true,
    task_selection: "issues-small",
    review_mode: "unattended",
    repetitions: 1,
  });
});

test("setup shows each suite's phase and opens the active smoke run", async ({
  page,
}) => {
  const run: any = structuredClone(base);
  run.status = "running";
  run.reviews = [];
  run.session_progress = {
    phase: "implementation",
    attempt_id: "issues-small-r1",
  };
  await setup(page, run, {
    phase: "qualifying",
    detail:
      "Offline preparation passed. 1 of 3 new suites ready. Live smoke tests run one at a time. Existing benchmark profiles remain available.",
    suites: {
      "coding-sessions": {
        phase: "running",
        run_id: run.id,
        detail: "issues-small-r1 · implementation",
        turns: 12,
        generation: { active: true, characters_received: 3142 },
      },
      "vision-checks": { phase: "ready" },
      "visual-design": {
        phase: "queued",
        detail: "Waiting for an idle machine",
      },
    },
  });
  const banner = page.getByRole("status", { name: "Benchmark suite setup" });
  await expect(banner).toContainText("Coding sessions: Running");
  await expect(banner).toContainText("Receiving response (3,142 characters)");
  await expect(banner).toContainText(
    "issues-small-r1 · implementation · 12 model turns",
  );
  await expect(banner).toContainText("Vision checks: Ready");
  await expect(banner).toContainText(
    "Visual design: Queued · Waiting for an idle machine",
  );
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/setup-progress-mobile.png",
    fullPage: true,
  });
  await page
    .getByRole("button", { name: "View Coding sessions smoke run" })
    .click();
  await expect(
    page.getByRole("button", { name: "Run again", exact: true }),
  ).toBeVisible();
});

test("finished setup retains failure evidence after reload", async ({
  page,
}) => {
  await setup(page, base, {
    phase: "needs_attention",
    detail:
      "Offline preparation passed. 1 of 3 new suites ready. Setup has stopped; select Start setup to retry the suites that have not passed.",
    can_start: true,
    can_stop: false,
    suites: {
      "coding-sessions": {
        phase: "failed",
        detail: "Command timed out after 120 seconds",
        run_id: base.id,
      },
      "vision-checks": { phase: "ready" },
      "visual-design": {
        phase: "not_passed",
        detail:
          "Active-time limit exhausted · 30.0 active minutes · 0 verification attempts. Open the run for evidence.",
        run_id: base.id,
      },
    },
  });
  await page.reload();
  await openSetup(page);
  const banner = page.getByRole("status", { name: "Benchmark suite setup" });
  await expect(banner).toContainText("Setup has stopped");
  await expect(banner).toContainText(
    "30.0 active minutes · 0 verification attempts",
  );
  await expect(banner).toContainText("Vision checks: Ready");
  await expect(
    banner.getByRole("button", { name: "Start setup", exact: true }),
  ).toBeVisible();
  await expect(
    banner.getByRole("button", { name: "Stop setup", exact: true }),
  ).toHaveCount(0);
});
