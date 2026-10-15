import { defineConfig, devices } from "@playwright/test";

const PORT = 47399;

export default defineConfig({
  testDir: "./e2e",
  timeout: 10 * 1000,
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], channel: "chrome" } }],
  // A real listener (make_app) over a throwaway store seeded with one fixture artifact.
  webServer: {
    command: `uv run python e2e/serve_fixture.py ${PORT}`,
    url: `http://127.0.0.1:${PORT}/healthz`,
    reuseExistingServer: !process.env.CI,
    timeout: 20 * 1000,
    stdout: "pipe",
    stderr: "pipe",
  },
});
