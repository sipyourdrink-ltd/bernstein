#!/usr/bin/env node
"use strict";

const { execFileSync, execSync } = require("node:child_process");
const args = process.argv.slice(2);

function which(cmd) {
  try {
    return execSync(`command -v ${cmd}`, { encoding: "utf8", stdio: ["pipe", "pipe", "ignore"] }).trim();
  } catch {
    return null;
  }
}

function run(cmd, cmdArgs) {
  try {
    execFileSync(cmd, cmdArgs, { stdio: "inherit" });
    process.exit(0);
  } catch (err) {
    process.exit(err.status || 1);
  }
}

// Strategy 1: pipx run (isolated, no global install conflict)
if (which("pipx")) {
  run("pipx", ["run", "bernstein", ...args]);
}

// Strategy 2: uvx (fast, uv-based)
if (which("uvx")) {
  run("uvx", ["bernstein", ...args]);
}

// Strategy 3: direct python -m invocation (if already pip-installed)
const python = which("python3") || which("python");
if (python) {
  run(python, ["-m", "bernstein", ...args]);
}

console.error(
  "Error: bernstein requires Python 3.12+.\n" +
  "Install Python from https://www.python.org/ then run:\n" +
  "  pip install bernstein"
);
process.exit(1);
