import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 240_000,
  expect: {
    timeout: 20_000
  },
  use: {
    baseURL: process.env.PSKA_E2E_FRONTEND_URL || "http://127.0.0.1:5173",
    trace: "retain-on-failure",
    video: "retain-on-failure"
  }
});
