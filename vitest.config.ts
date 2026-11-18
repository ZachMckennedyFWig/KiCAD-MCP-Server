import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests-ts/**/*.test.ts"],
    environment: "node",
    reporters: ["verbose"],
    // Worker threads, not child processes: on Windows each forked worker would
    // open (and flash) a console window when vitest runs without a console.
    // Processes the tests start themselves pass windowsHide (hidden-windows.ts).
    pool: "threads",
  },
});
