import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests",
  projects: [
    { name: "localhost", use: { baseURL: "http://127.0.0.1:5173" } },
    {
      name: "lan-http",
      use: {
        baseURL: "http://bench-studio.test:5173",
        launchOptions: {
          args: ["--host-resolver-rules=MAP bench-studio.test 127.0.0.1"],
        },
      },
    },
  ],
  webServer: {
    command: "npm run dev -- --host 127.0.0.1",
    url: "http://127.0.0.1:5173",
    env: { __VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS: "bench-studio.test" },
    reuseExistingServer: !process.env.CI,
  },
  reporter: "list",
});
