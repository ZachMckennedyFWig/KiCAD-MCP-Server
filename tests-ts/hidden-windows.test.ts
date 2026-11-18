import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  HIDE_CONSOLE_WINDOWS_ENV,
  hideConsoleWindows,
  windowsHideOption,
} from "../src/hidden-windows.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");

describe("KICAD_MCP_HIDE_CONSOLE_WINDOWS", () => {
  it("hides windows by default", () => {
    expect(hideConsoleWindows({})).toBe(true);
    expect(windowsHideOption({})).toEqual({ windowsHide: true });
  });

  it("turns off only with 0/false/no/off", () => {
    for (const value of ["0", "false", "No", " off "]) {
      expect(windowsHideOption({ [HIDE_CONSOLE_WINDOWS_ENV]: value })).toEqual({});
    }
    for (const value of ["", "1", "true", "anything"]) {
      expect(hideConsoleWindows({ [HIDE_CONSOLE_WINDOWS_ENV]: value })).toBe(true);
    }
  });

  it("is applied to every process the bridge starts", () => {
    for (const file of ["src/server.ts", "src/kicad-server.ts"]) {
      const source = readFileSync(join(ROOT, file), "utf8");
      const launches = source.match(/\b(spawn|exec|execSync)\(/g) ?? [];
      const hidden = source.match(/windowsHideOption\(\)/g) ?? [];
      expect(hidden.length, file).toBe(launches.length);
    }
  });

  it("no other source, script or TypeScript test starts a process without hiding it", () => {
    // Every launch outside the bridge must pass windowsHide explicitly.
    const offenders: string[] = [];
    const dirs = ["src", "src/tools", "src/resources", "src/prompts", "scripts", "tests-ts"];
    for (const dir of dirs) {
      let names: string[];
      try {
        names = readdirSync(join(ROOT, dir));
      } catch {
        continue;
      }
      for (const name of names.filter((n) => /\.(ts|mjs|js)$/.test(n))) {
        const rel = `${dir}/${name}`;
        if (rel === "src/server.ts" || rel === "src/kicad-server.ts") continue;
        const source = readFileSync(join(ROOT, rel), "utf8");
        for (const m of source.matchAll(
          /\b(spawn|spawnSync|execSync|execFileSync|execFile|fork)\(|new StdioClientTransport\(/g,
        )) {
          const call = source.slice(m.index, m.index + 400);
          if (!/windowsHide/.test(call)) offenders.push(`${rel}: ${m[0]}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});
