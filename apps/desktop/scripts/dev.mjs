import { spawn } from "node:child_process";

const npmCommand = process.platform === "win32" ? "npm.cmd" : "npm";
const electronCommand = process.platform === "win32" ? "electron.cmd" : "electron";

await run(npmCommand, ["run", "build:main"]);
const vite = spawn(npmCommand, ["exec", "vite", "--", "--host", "127.0.0.1", "--port", "5173", "--strictPort"], {
  stdio: "inherit",
  shell: false,
});

try {
  await waitForDevServer();
  const electron = spawn(electronCommand, ["."], {
    cwd: new URL("..", import.meta.url),
    env: { ...process.env, JELICA_DESKTOP_DEV: "1" },
    stdio: "inherit",
    shell: false,
  });
  const exitCode = await onExit(electron);
  process.exitCode = exitCode;
} finally {
  vite.kill();
}

function run(command, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: "inherit", shell: false });
    child.once("error", reject);
    child.once("exit", (code) => (code === 0 ? resolve() : reject(new Error(`${command} exited with ${code ?? "unknown"}`))));
  });
}

async function waitForDevServer() {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    try {
      const response = await fetch("http://127.0.0.1:5173", { signal: AbortSignal.timeout(500) });
      if (response.ok) return;
    } catch {
      // The fixed loopback development server is still starting.
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("The fixed JELICA Desktop development server did not start.");
}

function onExit(child) {
  return new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("exit", (code) => resolve(code ?? 1));
  });
}
