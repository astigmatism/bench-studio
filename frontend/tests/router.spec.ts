import { test, expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
const profiles = JSON.parse(
  readFileSync(new URL("./profiles.json", import.meta.url), "utf8"),
);
// Shapes from GET /api/models, which reads the LLM Router capabilities document.
const daytime = {
  alias: "daytime",
  service: "daytime",
  canonical: "qwen3.8-flash-next",
  display_name: "Qwen3.8 Flash-Next (128K)",
  configuration: "flash-next-solo-128k",
  fingerprint: "daytime-identity",
  context: 131072,
  available: true,
  capability_score: 77.8,
  nsfw: false,
  gpus: ["RTX 3090"],
  reasoning: { efforts: { off: "none", low: "low" } },
};
const nighttime = {
  alias: "nighttime",
  service: "nighttime",
  canonical: "qwen3.8-27b-abliterated-q6_k",
  configuration: "qwen27b-q6k-with-nighttime",
  fingerprint: "nighttime-identity",
  context: 98304,
  available: true,
  capability_score: 64.9,
  nsfw: true,
  gpus: ["RTX 4080"],
  reasoning: { efforts: { off: "none" } },
};
const solo = {
  runtime: { ready: true },
  router: { configuration: "flash-next-solo-128k", switching: false },
  offline_services: [
    {
      service: "nighttime",
      aliases: ["nighttime"],
      model: "qwen3.8-27b-abliterated-q6_k",
      display_name: "Qwen3.8 27B Abliterated Q6_K (96K)",
      reason: "exclusive_configuration",
    },
  ],
  models: [daytime],
};
const paired = {
  runtime: { ready: true },
  router: { configuration: "qwen27b-q6k-with-nighttime", switching: false },
  offline_services: [],
  models: [
    { ...daytime, configuration: "qwen27b-q6k-with-nighttime" },
    nighttime,
  ],
};
async function serve(page: Page, models: () => any, events: string) {
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/models") return route.fulfill({ json: models() });
    if (path === "/api/events")
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: events,
      });
    if (path === "/api/profiles") return route.fulfill({ json: profiles });
    if (path === "/api/health")
      return route.fulfill({ json: { ok: true, runner: {} } });
    if (path === "/api/runs") return route.fulfill({ json: [] });
    return route.fulfill({ json: {} });
  });
}
test("model cards show service, configuration, capability score and NSFW", async ({
  page,
}) => {
  await serve(page, () => paired, "retry: 60000\n\n");
  await page.goto("/");
  await page.getByRole("button", { name: "New benchmark", exact: true }).click();
  const night = page.getByRole("button", { name: /Nighttime\s*NSFW/ });
  await expect(night).toContainText("Service nighttime");
  await expect(night).toContainText("configuration qwen27b-q6k-with-nighttime");
  await expect(night).toContainText("capability score 64.9");
  const day = page.getByRole("button", { name: /Daytime qwen3.8-flash-next/ });
  await expect(day).not.toContainText("NSFW");
  await expect(day).toContainText("capability score 77.8");
  await expect(page.getByTestId("offline-services")).toHaveCount(0);
});
test("offline services are listed with their reason and never substituted", async ({
  page,
}) => {
  await serve(page, () => solo, "retry: 60000\n\n");
  await page.goto("/");
  await expect(page.getByText(/flash-next-solo-128k/).first()).toBeVisible();
  await page.getByRole("button", { name: "New benchmark", exact: true }).click();
  const offline = page.getByTestId("offline-services");
  await expect(offline).toContainText(
    "Offline in configuration flash-next-solo-128k",
  );
  await expect(offline).toContainText("Nighttime");
  await expect(offline).toContainText("exclusive_configuration");
  await expect(page.getByRole("button", { name: /^Nighttime/ })).toHaveCount(0);
});
test("router switching is shown while the router drains", async ({ page }) => {
  await serve(
    page,
    () => ({
      ...paired,
      router: {
        ...paired.router,
        switching: true,
        switching_reason: "router switching configuration",
      },
      models: paired.models.map((m) => ({
        ...m,
        available: false,
        error: "router switching configuration",
      })),
    }),
    "retry: 60000\n\n",
  );
  await page.goto("/");
  await expect(page.getByText("router switching configuration").first()).toBeVisible();
  await page.getByRole("button", { name: "New benchmark", exact: true }).click();
  await expect(page.getByTestId("router-switching")).toContainText(
    "Router switching configuration",
  );
});
test("a router change event refreshes the model list", async ({ page }) => {
  let calls = 0;
  await serve(
    page,
    () => (++calls === 1 ? paired : solo),
    'retry: 60000\n\ndata: {"type":"router_capabilities","configuration":"flash-next-solo-128k"}\n\n',
  );
  await page.goto("/");
  await expect(page.getByText(/flash-next-solo-128k/).first()).toBeVisible();
  expect(calls).toBeGreaterThanOrEqual(2);
});
