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
        ? [value]
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
  await expect(history.getByText("28.0 tok/s")).toBeVisible();
  await expect(history.getByText("Model #1 of 2")).toBeVisible();
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

test("multi-model history keeps each model and its measurements in one row", async ({
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
  await expect(
    page.getByLabel("Select performance-run", { exact: true }),
  ).toHaveCount(1);
  await page.getByLabel("Select performance-run", { exact: true }).check();
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
