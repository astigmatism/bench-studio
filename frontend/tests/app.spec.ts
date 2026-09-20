import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
const profiles = JSON.parse(
  readFileSync(new URL("./profiles.json", import.meta.url), "utf8"),
);
const resolved = { canonical: "Qwen coding model", context: 131072 };
const completed = {
  id: "completed-001",
  profile: "coding",
  family: "speed",
  profile_spec: profiles.find((p: any) => p.id === "coding"),
  status: "completed",
  created_at: "2026-09-17T10:00:00Z",
  started_at: "2026-09-17T10:00:00Z",
  finished_at: "2026-09-17T10:02:00Z",
  mode: "sequential",
  requested_targets: ["daytime"],
  resolved: { daytime: resolved },
  summary: {
    daytime: {
      score: 42,
      unit: "tok/s",
      metric: "decode_tps",
      count: 5,
      metrics: [{ category: "code", decode_med: 42 }],
      tasks: [{ id: "code/1", status: "passed", decode_tps: 42, ttft_ms: 100 }],
    },
  },
  artifacts: ["daytime/decode.html"],
};
test.beforeEach(async ({ page }) => {
  let runs: any[] = [structuredClone(completed)];
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    let body: any = {};
    const path = url.pathname;
    if (path === "/api/health")
      body = { ok: true, revision: "test-revision", runner: {} };
    else if (path === "/api/models")
      body = {
        runtime: { ready: true },
        models: ["daytime", "nighttime"].map((alias) => ({
          alias,
          ...resolved,
          available: true,
          gpus: ["GPU A", "GPU B"],
          reasoning: { efforts: { off: "none", low: "low" } },
        })),
      };
    else if (path === "/api/profiles") body = profiles;
    else if (path === "/api/events")
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: "retry: 60000\n\n",
      });
    else if (path === "/api/runs" && route.request().method() === "POST") {
      const p = route.request().postDataJSON();
      body = {
        ...completed,
        id: "new-run",
        requested_targets: p.targets,
        status: "queued",
        summary: {},
        progress: "Waiting for an idle machine",
        note: p.note,
      };
      runs = [body, ...runs];
    } else if (path === "/api/runs") body = runs;
    else if (path.endsWith("/logs"))
      body = { text: "Connected to model\nMeasured request 1 / 5", size: 45 };
    else if (path.endsWith("/cancel")) {
      runs[0].status = "cancelled";
      body = runs[0];
    } else if (path.endsWith("/baseline")) body = { ok: true };
    else if (path === "/api/compare")
      body = {
        a: completed.summary.daytime,
        b: completed.summary.daytime,
        differences: [],
        warning: "Independent runs",
      };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });
  await page.goto("/");
});
test("launch both models, view live logs, cancel, and review rerun", async ({
  page,
}) => {
  await page
    .getByRole("button", { name: "New benchmark", exact: true })
    .click();
  await page.getByRole("button", { name: /Nighttime Qwen/ }).click();
  await expect(page.getByLabel("Run both models")).toHaveValue("sequential");
  await page.getByLabel("Run note").fill("Repeatable baseline");
  await page.getByRole("button", { name: "Queue benchmark" }).click();
  await expect(
    page.getByRole("heading", { name: "Coding throughput", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Waiting for an idle machine")).toBeVisible();
  await page.getByRole("button", { name: "Logs", exact: true }).click();
  await expect(page.getByText(/Measured request 1/)).toBeVisible();
  page.on("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Stop", exact: true }).click();
  await expect(page.getByText("Cancelled", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Run again" }).click();
  await expect(
    page.getByText(/Review the currently loaded models/),
  ).toBeVisible();
});
test("profiles, keyboard operation, history, artifacts, comparison", async ({
  page,
}) => {
  await page.getByRole("button", { name: "Profiles", exact: true }).focus();
  await page.keyboard.press("Enter");
  await expect(
    page.getByRole("heading", { name: "A profile for every question." }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Repository tasks", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Runs", exact: true }).click();
  await page
    .getByRole("button", { name: "Coding throughput", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Reports & exports", exact: true })
    .click();
  await expect(
    page.getByRole("link", { name: "Export bundle" }),
  ).toHaveAttribute("href", /export$/);
  await expect(
    page.getByRole("link", { name: "daytime/decode.html" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Compare", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Did the change help?" }),
  ).toBeVisible();
});
test("narrow and dark layouts stay usable", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ colorScheme: "dark" });
  await page
    .getByRole("button", { name: "New benchmark", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Queue benchmark" }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/launch-mobile-dark.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 1440, height: 1050 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.screenshot({
    path: "test-results/launch-desktop.png",
    fullPage: true,
  });
});

test("failed repository runs retain partial task evidence without a headline score", async ({
  page,
}) => {
  const run = {
    ...completed,
    id: "failed-repository",
    family: "agent",
    profile: "repository-tasks",
    profile_spec: profiles.find((p: any) => p.id === "repository-tasks"),
    status: "failed",
    error: "PermissionError: oracle log unreadable",
    summary: {
      daytime: {
        score: null,
        unit: "%",
        partial: true,
        passed: 1,
        count: 2,
        completed_count: 1,
        tasks: [
          {
            id: "fixed-task",
            status: "passed",
            trial_path: "harbor/job/trial/result.json",
          },
          {
            id: "pending-task",
            status: "not_completed",
            detail: "No completed trial",
          },
        ],
      },
    },
  };
  await page.route("**/api/runs", (route) => route.fulfill({ json: [run] }));
  await page.reload();
  await page
    .getByRole("button", { name: "Repository tasks", exact: true })
    .click();
  await expect(page.getByText(/Partial evidence: 1 of 2/)).toBeVisible();
  await expect(
    page.getByText("PermissionError: oracle log unreadable"),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Trial evidence" }),
  ).toHaveAttribute("href", /failed-repository\/artifacts\/daytime\/harbor/);
  await expect(
    page.getByRole("button", { name: "Set baseline" }),
  ).toBeDisabled();
});

test("coding details distinguish exhausted output and repetitive reasoning", async ({
  page,
}) => {
  const run = {
    ...completed,
    family: "quality",
    profile: "coding-checks",
    profile_spec: profiles.find((p: any) => p.id === "coding-checks"),
    summary: {
      daytime: {
        score: 0,
        unit: "%",
        passed: 0,
        count: 1,
        repetition_count: 1,
        tasks: [
          {
            id: "task",
            status: "failed",
            finish_reason: "length",
            detail: "Output budget exhausted; no final code returned",
            diagnostics: { answer_chars: 0, reasoning_chars: 9000 },
            usage: { completion_tokens: 8192 },
          },
        ],
      },
    },
  };
  await page.route("**/api/runs", (route) => route.fulfill({ json: [run] }));
  await page.reload();
  await page
    .getByRole("button", { name: "Function checks", exact: true })
    .click();
  await expect(
    page.getByText(/1 answers exhausted the total output budget/),
  ).toBeVisible();
  await expect(
    page.getByText(/1 responses showed repetitive reasoning/),
  ).toBeVisible();
  await expect(page.getByText(/0 answer characters/)).toBeVisible();
});

test("coding launch explains shared output allowance", async ({ page }) => {
  await page
    .getByRole("button", { name: "New benchmark", exact: true })
    .click();
  await page
    .getByRole("button", { name: /Function checks Solutions graded/ })
    .click();
  await page.getByText("Advanced parameters · profile defaults").click();
  await expect(
    page.getByLabel("Total output tokens (thinking + answer)"),
  ).toHaveValue("8192");
  await expect(
    page.getByText(/The total output limit includes thinking/),
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});

test("select all, compare two, cancel and delete chosen history with active runs preserved", async ({
  page,
}) => {
  let runs = [
    { ...completed, id: "active-run", status: "running", progress: "Working" },
    { ...completed, id: "run-a" },
    { ...completed, id: "run-b", status: "failed" },
    { ...completed, id: "run-c", status: "cancelled" },
  ];
  const deleted: string[][] = [];
  await page.route("**/api/runs", (route) => route.fulfill({ json: runs }));
  await page.route("**/api/runs/delete", (route) => {
    const { ids } = route.request().postDataJSON();
    deleted.push(ids);
    runs = runs.filter((r) => !ids.includes(r.id));
    return route.fulfill({ json: { deleted: ids, cleanup_failed: [] } });
  });
  await page.reload();
  const all = page.getByLabel("Select all runs", { exact: true });
  await expect(
    page.getByLabel("Select active-run", { exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Delete selected", exact: true }),
  ).toBeDisabled();
  await page.getByLabel("Select run-a", { exact: true }).check();
  await expect(all).toBeChecked({ indeterminate: true });
  await page.getByLabel("Select run-b", { exact: true }).check();
  await expect(
    page.getByRole("button", { name: "Compare selected" }),
  ).toBeEnabled();
  await all.check();
  await expect(
    page.getByRole("button", { name: "Compare selected" }),
  ).toBeDisabled();
  await expect(page.getByText("3 selected", { exact: true })).toBeVisible();
  await all.uncheck();
  await expect(page.getByText("0 selected", { exact: true })).toBeVisible();
  await all.focus();
  await page.keyboard.press("Space");
  await page
    .getByRole("button", { name: "Delete selected", exact: true })
    .click();
  const dialog = page.getByRole("dialog", { name: "Delete 3 runs?" });
  await expect(dialog).toContainText("This cannot be undone.");
  await expect(
    dialog.getByRole("button", { name: "Cancel", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  expect(deleted).toEqual([]);
  await page.getByLabel("Select run-c", { exact: true }).uncheck();
  await page
    .getByRole("button", { name: "Delete selected", exact: true })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Delete 2 runs", exact: true })
    .click();
  await expect(
    page.getByText("Deleted 2 runs.", { exact: true }),
  ).toBeVisible();
  expect(deleted).toEqual([["run-a", "run-b"]]);
  await expect(page.getByLabel("Select run-a", { exact: true })).toHaveCount(0);
  await expect(
    page.getByLabel("Select run-c", { exact: true }),
  ).not.toBeChecked();
  await expect(page.getByText("Working", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("Select run-a", { exact: true })).toHaveCount(0);
  await all.check();
  await page
    .getByRole("button", { name: "Delete selected", exact: true })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Delete 1 run", exact: true })
    .click();
  await expect(
    page.getByText("No finished runs yet. Your results will appear here."),
  ).toBeVisible();
  await expect(all).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "View run", exact: true }),
  ).toBeVisible();
});

test("deletion errors preserve selection and pending deletion cannot be submitted twice", async ({
  page,
}) => {
  let calls = 0;
  let respond!: () => void;
  const pending = new Promise<void>((resolve) => {
    respond = resolve;
  });
  await page.route("**/api/runs/delete", async (route) => {
    calls++;
    await pending;
    return route.fulfill({
      status: 409,
      json: { detail: "Application update in progress" },
    });
  });
  await page.getByLabel("Select all runs", { exact: true }).check();
  await page
    .getByRole("button", { name: "Delete selected", exact: true })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Delete 1 run", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Deleting…", exact: true }),
  ).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeVisible();
  respond();
  await expect(page.getByRole("dialog").getByRole("alert")).toContainText(
    "Application update in progress",
  );
  expect(calls).toBe(1);
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(
    page.getByLabel("Select completed-001", { exact: true }),
  ).toBeChecked();
});

test("suite setup stays out of the workspace and expands only within Profiles", async ({
  page,
}) => {
  await page.route("**/api/health", (route) =>
    route.fulfill({
      json: {
        ok: true,
        session_setup: {
          phase: "ready",
          detail: "Fixtures validated. All benchmark suites are available.",
          can_start: true,
          can_stop: false,
          suites: {},
        },
      },
    }),
  );
  await page.reload();
  await expect(
    page.getByRole("status", { name: "Benchmark suite setup" }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Profiles", exact: true }).click();
  const setup = page.getByRole("status", { name: "Benchmark suite setup" });
  await expect(setup).not.toBeVisible();
  await page.locator("summary").filter({ hasText: "Suite setup" }).focus();
  await page.keyboard.press("Enter");
  await expect(setup).toBeVisible();
  await expect(
    setup.getByRole("button", { name: "Run optional model checks" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Runs", exact: true }).click();
  await expect(setup).toHaveCount(0);
  await page
    .getByRole("button", { name: "Coding throughput", exact: true })
    .click();
  await expect(setup).toHaveCount(0);
  await page.getByRole("button", { name: "Compare", exact: true }).click();
  await expect(setup).toHaveCount(0);
  await page.getByRole("button", { name: "Profiles", exact: true }).click();
  await expect(setup).not.toBeVisible();
});

test("history bulk actions and confirmation fit narrow and dark layouts", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ colorScheme: "dark" });
  await page.getByLabel("Select all runs", { exact: true }).check();
  const deleteButton = page.getByRole("button", {
    name: "Delete selected",
    exact: true,
  });
  await expect(deleteButton).toBeInViewport();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/history-mobile-dark.png",
    fullPage: true,
  });
  await deleteButton.click();
  await expect(page.getByRole("dialog")).toBeInViewport();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/delete-mobile-dark.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await page.setViewportSize({ width: 1440, height: 1050 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.screenshot({
    path: "test-results/history-desktop.png",
    fullPage: true,
  });
});

test("a history refresh started before deletion cannot restore deleted rows", async ({
  page,
}) => {
  let runs = [completed];
  let reads = 0;
  let holdNext = false;
  let held = false;
  let release!: () => void;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/runs", async (route) => {
    const snapshot = [...runs];
    reads++;
    if (holdNext) {
      holdNext = false;
      held = true;
      await pending;
    }
    return route.fulfill({ json: snapshot });
  });
  await page.route("**/api/runs/delete", (route) => {
    const { ids } = route.request().postDataJSON();
    runs = [];
    return route.fulfill({ json: { deleted: ids, cleanup_failed: [] } });
  });
  await page.clock.install();
  await page.reload();
  await page.getByLabel("Select all runs", { exact: true }).check();
  holdNext = true;
  await page.clock.fastForward(15001);
  await expect.poll(() => held).toBe(true);
  const beforeDelete = reads;
  await page
    .getByRole("button", { name: "Delete selected", exact: true })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Delete 1 run", exact: true })
    .click();
  await expect(
    page.getByText("No finished runs yet. Your results will appear here."),
  ).toBeVisible();
  await expect.poll(() => reads).toBeGreaterThan(beforeDelete);
  const staleResponse = page.waitForResponse(
    (r) => new URL(r.url()).pathname === "/api/runs",
  );
  release();
  await (await staleResponse).finished();
  await page.clock.runFor(100);
  await expect(
    page.getByLabel("Select completed-001", { exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByLabel("Select all runs", { exact: true }),
  ).toBeDisabled();
});
