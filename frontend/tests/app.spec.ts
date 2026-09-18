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
    page.getByRole("heading", { name: "Coding speed", exact: true }),
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
  await page.getByRole("button", { name: "Coding speed", exact: true }).click();
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
    .getByRole("button", { name: "Coding checks", exact: true })
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
    .getByRole("button", { name: /Coding checks Solutions graded/ })
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
