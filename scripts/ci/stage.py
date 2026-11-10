#!/usr/bin/env python3
"""Lumo CI stage script (DRAFT / untested).

Runs ONE build stage inside a single GitHub Actions job, Helium-style.

Flow (mirrors Helium's .github/actions/stage/index.js):
  1. If resuming (STAGE_FROM_ARTIFACT=1), the workflow has already
     downloaded and extracted the previous build-state artifact into
     the Chromium checkout.
  2. Run `gn gen` (first stage only) and `ninja` with a time budget.
  3. If the build finishes within the budget, exit 0 and set
     finished=true in $GITHUB_OUTPUT.
  4. If the time budget runs out first, stop ninja cleanly, zip the
     build directory for the next job, and exit 42 (Helium's
     "incomplete, resume in the next stage" signal). The workflow
     then uploads the zip as an artifact and the next build-N job
     resumes from it.

This script has NEVER been run. It is a starting point, not a
working build script. Do not assume it works.
"""

import argparse
import os
import subprocess
import sys
import time

# Helium's convention: exit code 42 means "build incomplete, resume later".
EXIT_INCOMPLETE = 42
# Default time budget per job: 5 hours, safely under the 6h Actions limit.
DEFAULT_BUDGET_SECONDS = 5 * 60 * 60


def run(cmd, cwd, deadline):
    """Run cmd, return its exit code, or EXIT_INCOMPLETE on timeout."""
    proc = subprocess.Popen(cmd, cwd=cwd)
    while True:
        try:
            return proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            if time.time() >= deadline:
                proc.terminate()
                try:
                    proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return EXIT_INCOMPLETE


def set_output(name, value):
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    print(f"::set-output name={name}::{value}")


def main():
    parser = argparse.ArgumentParser(description="Lumo single build stage (DRAFT)")
    parser.add_argument("--chromium-src", required=True,
                        help="Path to the Chromium src checkout")
    parser.add_argument("--out-dir", default="out/lumo",
                        help="GN output dir relative to chromium src")
    parser.add_argument("--budget-seconds", type=int,
                        default=DEFAULT_BUDGET_SECONDS)
    parser.add_argument("--target", default="lumo",
                        help="Ninja target (e.g. chrome, lumo)")
    args = parser.parse_args()

    deadline = time.time() + args.budget_seconds

    # TODO: generate args.gn here from args.gn.example + Secrets
    # (GOOGLE_API_KEY / GOOGLE_DEFAULT_CLIENT_ID /
    #  GOOGLE_DEFAULT_CLIENT_SECRET) on the first stage only.
    # TODO: run `gn gen out/lumo` on the first stage only.
    # TODO: run ninja with the time budget and zip the build dir
    # (7z) when the budget expires, for the next stage to resume.
    print("DRAFT STUB: no real build is performed yet.", file=sys.stderr)
    print("TODO: gn gen + ninja with time budget, exit 42 when incomplete.",
          file=sys.stderr)
    set_output("finished", "false")
    return EXIT_INCOMPLETE


if __name__ == "__main__":
    sys.exit(main())
