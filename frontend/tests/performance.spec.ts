import { test, expect } from "@playwright/test";

const metrics = [
  ["output_tps", "Output speed", 28.045, "tok/s", "higher", 1],
  ["ttft_seconds", "First token", 4.823, "s", "lower", 2],
  ["prompt_tps", "Prompt processing", null, "tok/s", "higher", 1],
  ["request_seconds", "Request latency", 9.0677, "s", "lower", 2],
].map(([id, label, value, unit, direction, precision]) => ({
  id,
  label,
  value,
  unit,
  direction,
  precision,
  samples: value == null ? 0 : 50,
  total: 51,
  unavailable: value == null ? "Not exposed" : null,
  model_rank: value == null ? null : { rank: 1, total: 2 },
  overall_rank: value == null ? null : { rank: 2, total: 3 },
  previous:
    value == null
      ? null
      : {
          run_id: "prior",
          target: "daytime",
          improvement: id === "ttft_seconds" ? -12 : 10,
          unit: "%",
          differences: [{ field: "context", a: 16384, b: 32768 }],
        },
}));
const run: any = {
  id: "performance-run",
  profile: "coding-sessions",
  family: "session",
  status: "completed",
  created_at: "2026-09-20T12:00:00Z",
  finished_at: "2026-09-20T12:20:00Z",
  mode: "sequential",
  requested_targets: ["daytime"],
  resolved: { daytime: { canonical: "Model Alpha" } },
  profile_spec: { family: "session", name: "Coding sessions", repetitions: 1 },
  summary: {
    daytime: {
      score: 100,
      unit: "%",
      count: 2,
      passed: 2,
      tasks: [],
      metrics: [],
      performance: {
        metrics,
        requests: 51,
        request_errors: 1,
        output_limit_requests: 0,
      },
    },
  },
  session_progress: { target: "daytime", phase: "verification", turns: 33 },
};

async function setup(page: any, value = run) {
  await page.route("**/api/**", async (route: any) => {
    const path = new URL(route.request().url()).pathname;
    const data =
      path === "/api/runs"
        ? Array.isArray(value)
          ? value
          : [value]
        : path.startsWith("/api/runs/")
          ? value
          : path === "/api/profiles"
            ? []
            : path === "/api/models"
              ? { models: [], runtime: { ready: true } }
              : { ok: true };
    if (path === "/api/events")
      return route.fulfill({
        contentType: "text/event-stream",
        body: "retry: 60000\n\n",
      });
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(data),
    });
  });
  await page.goto("/");
}

test("performance leads completed runs with coverage, ranks and direction-aware changes", async ({
  page,
}) => {
  await setup(page);
  const history = page.getByRole("table");
  await expect(
    history.getByRole("columnheader", { name: "Output speed" }),
  ).toBeVisible();
  await expect(
    history.getByRole("columnheader", { name: "Input speed" }),
  ).toBeVisible();
  await expect(history.getByText("28.0 tok/s")).toBeVisible();
  await expect(history.getByText("Model #1 of 2")).toHaveCount(0);
  await page
    .getByRole("button", { name: "Coding sessions", exact: true })
    .click();
  const cards = page.getByRole("region", {
    name: "Performance scorecard",
    exact: true,
  });
  await expect(cards.getByText("28.0 tok/s")).toBeVisible();
  await expect(cards.getByText("4.82 s")).toBeVisible();
  await expect(cards.getByText("9.07 s")).toBeVisible();
  await expect(cards.getByText("Not exposed")).toBeVisible();
  await expect(cards.getByText("12.0% worse vs previous")).toBeVisible();
  await expect(
    cards.getByText("50 / 51 requests · Higher is better"),
  ).toBeVisible();
  const firstCard = cards.getByRole("region", {
    name: "Output speed",
    exact: true,
  });
  await firstCard.getByText("Comparison conditions").click();
  await expect(firstCard.getByText("16384 → 32768")).toBeVisible();
  const y = await cards.boundingBox();
  const timeline = await page
    .getByRole("heading", { name: "Session timeline" })
    .boundingBox();
  expect(y!.y).toBeLessThan(timeline!.y);
  await expect(page.getByText("Session timing and token totals")).toBeVisible();
});

test("multi-model history gives each model its own row in one table", async ({
  page,
}) => {
  const value = structuredClone(run);
  value.requested_targets.push("nighttime");
  value.resolved.nighttime = { canonical: "Model Beta" };
  value.summary.nighttime = structuredClone(value.summary.daytime);
  value.summary.nighttime.performance.metrics[0].value = 40;
  await setup(page, value);
  const alpha = page.getByRole("row").filter({ hasText: "Model Alpha" });
  const beta = page.getByRole("row").filter({ hasText: "Model Beta" });
  await expect(alpha.getByText("28.0 tok/s")).toBeVisible();
  await expect(beta.getByText("40.0 tok/s")).toBeVisible();
  // One row per run + model pair, so the run-level checkbox repeats per row.
  await expect(
    page.getByLabel("Select performance-run", { exact: true }),
  ).toHaveCount(2);
  await page
    .getByLabel("Select performance-run", { exact: true })
    .first()
    .check();
  await expect(
    page.getByRole("button", { name: "Delete selected" }),
  ).toBeEnabled();
});

test("first-run unavailable values and narrow layouts remain usable", async ({
  page,
}) => {
  const value = structuredClone(run);
  for (const m of value.summary.daytime.performance.metrics) {
    m.previous = null;
    if (m.model_rank) m.model_rank = m.overall_rank = { rank: 1, total: 1 };
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ colorScheme: "dark" });
  await setup(page, value);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page
    .getByRole("button", { name: "Coding sessions", exact: true })
    .click();
  await expect(
    page.getByText("No previous matching run").first(),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/performance-mobile-dark.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.emulateMedia({ colorScheme: "light" });
  await page.screenshot({
    path: "test-results/performance-desktop.png",
    fullPage: true,
  });
});

test("active sessions retain prominent progress and no completed scorecard", async ({
  page,
}) => {
  const value = structuredClone(run);
  value.status = "running";
  value.progress = "Generating response";
  await setup(page, value);
  await page.getByRole("button", { name: "View run" }).click();
  await expect(
    page.getByRole("heading", { name: "Session progress" }),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "Performance scorecard" }),
  ).toHaveCount(0);
});

test("input speed shows the estimate for regular runs and the prefill score for sweeps", async ({
  page,
}) => {
  const value = structuredClone(run);
  value.summary.daytime.performance.metrics.push({
    id: "prompt_estimate_tps",
    label: "Prompt throughput estimate",
    value: 1234.5,
    unit: "tok/s",
    direction: "higher",
    precision: 1,
    samples: 50,
    total: 51,
  });
  await setup(page, value);
  const history = page.getByRole("table");
  await expect(history.getByText("1,234.5 tok/s")).toBeVisible();
  await expect(
    history.getByRole("columnheader", { name: "First token", exact: true }),
  ).toHaveCount(0);
  await expect(
    history.getByRole("columnheader", {
      name: "Prompt processing",
      exact: true,
    }),
  ).toHaveCount(0);
  const sweep = structuredClone(value);
  sweep.summary.daytime.metric = "prefill_tps";
  sweep.summary.daytime.score = 555;
  sweep.summary.daytime.unit = "prompt tok/s";
  await setup(page, sweep);
  await expect(
    page.getByRole("table").getByText("555.0 prompt tok/s"),
  ).toBeVisible();
});

test("history tables sort by any column", async ({ page }) => {
  const fast = structuredClone(run);
  fast.id = "sort-fast";
  fast.created_at = "2026-09-21T11:00:00Z";
  fast.resolved.daytime = { canonical: "Model A" };
  fast.summary.daytime.performance.metrics[0].value = 40;
  const slow = structuredClone(run);
  slow.id = "sort-slow";
  slow.created_at = "2026-09-21T12:00:00Z";
  slow.resolved.daytime = { canonical: "Model Z" };
  slow.summary.daytime.performance.metrics[0].value = 20;
  await setup(page, [slow, fast]);
  const rows = page
    .getByRole("region", { name: "Coding sessions" })
    .getByRole("row");
  const indexOf = async (model: string) => {
    const texts = await rows.allTextContents();
    return texts.findIndex((t) => t.includes(model));
  };
  // Default order is newest first: slow (Model Z) is newer, so it leads.
  await expect(
    page.getByRole("columnheader", { name: "Run", exact: true }),
  ).toHaveAttribute("aria-sort", "descending");
  expect(await indexOf("Model Z")).toBeLessThan(await indexOf("Model A"));
  // Numeric columns sort descending on first click: 40 tok/s leads.
  const header = page.getByRole("button", { name: /Output speed/ });
  await header.click();
  await expect(
    page.getByRole("columnheader", { name: /Output speed/ }),
  ).toHaveAttribute("aria-sort", "descending");
  expect(await indexOf("Model A")).toBeLessThan(await indexOf("Model Z"));
  await header.click();
  await expect(
    page.getByRole("columnheader", { name: /Output speed/ }),
  ).toHaveAttribute("aria-sort", "ascending");
  expect(await indexOf("Model Z")).toBeLessThan(await indexOf("Model A"));
  // Text columns sort ascending on first click.
  await page.getByRole("button", { name: "Model", exact: true }).click();
  await expect(
    page.getByRole("columnheader", { name: /Model/ }),
  ).toHaveAttribute("aria-sort", "ascending");
  expect(await indexOf("Model A")).toBeLessThan(await indexOf("Model Z"));
});

test("history keeps one table per benchmark", async ({ page }) => {
  const first = structuredClone(run);
  first.id = "bench-a";
  const second = structuredClone(run);
  second.id = "bench-b";
  second.profile = "coding";
  second.profile_spec = { family: "speed", name: "Coding throughput" };
  second.resolved.daytime = { canonical: "Model Beta" };
  await setup(page, [first, second]);
  const sessions = page.getByRole("region", { name: "Coding sessions" });
  const coding = page.getByRole("region", {
    name: "Coding throughput",
    exact: true,
  });
  await expect(sessions).toContainText("Model Alpha");
  await expect(sessions).not.toContainText("Model Beta");
  await expect(coding).toContainText("Model Beta");
  await expect(coding).not.toContainText("Model Alpha");
  await expect(
    page.getByRole("columnheader", { name: "Output speed" }),
  ).toHaveCount(2);
});

test("per-table select all selects only its category and spans tables with the global bar", async ({
  page,
}) => {
  const first = structuredClone(run);
  first.id = "cat-a";
  const second = structuredClone(run);
  second.id = "cat-b";
  second.profile = "coding";
  second.profile_spec = { family: "speed", name: "Coding throughput" };
  await setup(page, [first, second]);
  await page
    .getByLabel("Select all Coding sessions runs", { exact: true })
    .check();
  await expect(page.getByText("1 selected", { exact: true })).toBeVisible();
  await page
    .getByLabel("Select all Coding throughput runs", { exact: true })
    .check();
  await expect(page.getByText("2 selected", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Compare selected", exact: true }),
  ).toBeEnabled();
  await page.getByLabel("Select all runs", { exact: true }).uncheck();
  await expect(page.getByText("0 selected", { exact: true })).toBeVisible();
});
