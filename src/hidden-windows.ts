/**
 * Start child processes without a console window on Windows (on by default).
 *
 * Under an MCP client the server has no console of its own, so every console
 * program it starts (the Python worker, version checks) would get a new window
 * that flashes up and takes the focus. Node's `windowsHide` is set on those
 * launches; the Python worker does the same for kicad-cli, Java and its other
 * children (python/utils/hidden_windows.py), since it inherits the setting.
 * KICAD_MCP_HIDE_CONSOLE_WINDOWS=0 (or false/no/off) turns it off.
 */
export const HIDE_CONSOLE_WINDOWS_ENV = "KICAD_MCP_HIDE_CONSOLE_WINDOWS";

const FALSE = new Set(["0", "false", "no", "off"]);

/** Whether windows are hidden: yes unless the setting says 0/false/no/off. */
export function hideConsoleWindows(env: Record<string, string | undefined> = process.env): boolean {
  return !FALSE.has((env[HIDE_CONSOLE_WINDOWS_ENV] ?? "").trim().toLowerCase());
}

/** Spread into spawn/exec options: `{ windowsHide: true }` unless the setting is off. */
export function windowsHideOption(env: Record<string, string | undefined> = process.env): {
  windowsHide?: boolean;
} {
  return hideConsoleWindows(env) ? { windowsHide: true } : {};
}
