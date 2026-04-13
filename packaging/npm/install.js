#!/usr/bin/env node
"use strict";

const { execSync } = require("node:child_process");

function which(cmd) {
  try {
    return execSync(`command -v ${cmd}`, { encoding: "utf8", stdio: ["pipe", "pipe", "ignore"] }).trim();
  } catch {
    return null;
  }
}

function checkPythonVersion() {
  const python = which("python3") || which("python");
  if (!python) return false;
  try {
    const version = execSync(`${python} -c "import sys; print(sys.version_info[:2])"`, {
      encoding: "utf8",
      stdio: ["pipe", "pipe", "ignore"],
    }).trim();
    // version looks like "(3, 12)"
    const match = version.match(/\((\d+),\s*(\d+)\)/);
    if (match) {
      const [, major, minor] = match.map(Number);
      return major >= 3 && minor >= 12;
    }
  } catch {}
  return false;
}

const hasPipx = !!which("pipx");
const hasUvx = !!which("uvx");
const hasPython = checkPythonVersion();

if (!hasPython) {
  console.warn(
    "\n" +
    "  WARNING: bernstein requires Python >= 3.12\n" +
    "  Install Python from https://www.python.org/\n"
  );
  process.exit(0); // don't fail npm install
}

if (!hasPipx && !hasUvx) {
  console.warn(
    "\n" +
    "  TIP: Install pipx for best experience:\n" +
    "    python3 -m pip install --user pipx\n" +
    "    pipx ensurepath\n" +
    "\n" +
    "  Alternatively, install uv: https://docs.astral.sh/uv/\n" +
    "\n" +
    "  Without pipx/uvx, bernstein will fall back to python -m bernstein\n" +
    "  (requires: pip install bernstein)\n"
  );
}

console.log("bernstein-orchestrator: Python runtime check passed.");
