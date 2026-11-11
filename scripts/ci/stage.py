#!/usr/bin/env python3
"""Lumo CI stage script.

Runs ONE build stage inside a single GitHub Actions job, Helium-style.

Pipeline (all inside the job's time budget):
  1. bootstrap -- clone depot_tools + Thorium (pinned commit), then run
     Thorium's get_repo.py to fetch Chromium (fetch + gclient sync +
     runhooks). A partial fetch is resumed by the next stage via
     get_repo.py --recover-incomplete.
  2. version   -- Thorium's version.py: check out the pinned Chromium tag
     (156.0.8078.80) and gclient sync.
  3. setup     -- Thorium's setup.py --avx2: copy overlays, apply the
     Thorium patch series, GRD/XTB rebase.
  4. gn        -- write out/thorium/args.gn from args.gn.example, injecting
     the Google API keys from the environment (Secrets), then `gn gen`.
  5. build     -- autoninja thorium_all + thorium_installer with a time
     budget. The Thorium patches rename the binary to thorium.exe; the
     rename to lumo.exe happens at packaging time (build-final job).
     Profile is AVX2 only (target machine: Intel Core i5-8350U).

Resume model (mirrors Helium's stage action):
  * The whole build root (C:/lumo-build: Chromium src + .gclient +
    lumo-stage-state.json) is zipped with 7z and uploaded as an artifact
    when the time budget expires. The next stage downloads and extracts
    it and continues from the phase recorded in lumo-stage-state.json.
    Thorium (C:/thorium) and depot_tools (C:/depot_tools) live OUTSIDE
    the build root as siblings: Thorium's get_repo.py rejects overlapping
    checkout paths, and it treats the Chromium root as the parent of the
    src dir, so thorium/depot_tools must not sit under C:/lumo-build.
    They are re-cloned by each stage's bootstrap phase when missing.
  * Exit code 42 means "build incomplete, resume in the next stage"
    (Helium's convention). It is NOT a failure.
  * Exit code 0 means the build finished; thorium.exe was verified to
    exist. The state artifact is still uploaded so build-final can
    download the finished tree.
  * Any other non-zero exit code is a real failure and fails the job.

UNVERIFIED (has never run on GitHub Actions):
  * The full fetch + build has never been attempted; the first real run
    will almost certainly surface problems (SDK/toolchain versions,
    artifact size limits, disk space, patch application on the pinned
    tag, gn args, ninja edge cases).
  * The build-state zip can be tens of GB. GitHub Artifacts may refuse
    it, and zipping/uploading may eat the time reserve.
  * GitHub-hosted Windows runners start with only ~29 GiB free; stage.py
    removes unused preinstalled toolchains (Android SDK, Java, Haskell)
    to make room, but the full checkout + build may still not fit.
  * Renaming thorium.exe to lumo.exe at packaging time is assumed to
    work for a portable build; it has never been tested.
  * Thorium's own branding (About page, installer product name, etc.)
    still says Thorium. Full Lumo rebranding needs separate patches
    (see PLAN.md).
"""

import argparse
import ctypes
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path, PureWindowsPath

# Helium's convention: exit code 42 means "build incomplete, resume later".
EXIT_INCOMPLETE = 42
EXIT_FAILURE = 1

# Pinned inputs. Thorium commit 382f9a88 is the main-branch commit whose
# version.py declares THORIUM_VERSION = "156.0.8078.80" (verified locally
# 2026-11-10). Pin both so the build is reproducible.
THORIUM_REPO = "https://github.com/Alex313031/thorium.git"
DEFAULT_THORIUM_COMMIT = "382f9a882a4dce4e308f1aef71dad6f648e5a081"
DEFAULT_CHROMIUM_TAG = "156.0.8078.80"
DEPOT_TOOLS_REPO = "https://chromium.googlesource.com/chromium/tools/depot_tools.git"

# Thorium's GN output directory. setup.py copies pak/thor_ver into
# out/thorium and the mini_installer build expects thor_ver there, so the
# GN out dir MUST be out/thorium (not out/lumo).
OUT_DIR_NAME = "out/thorium"
STATE_FILE_NAME = "lumo-stage-state.json"
FETCH_MARKER = ".thorium-fetch-incomplete"

# Default work budget per job: 4h45m. The job timeout is 330 minutes
# (5.5h); the remaining ~45 minutes are reserved for zipping the build
# state and uploading the artifact. Tunable via --budget-seconds.
DEFAULT_BUDGET_SECONDS = 4 * 60 * 60 + 45 * 60
# Absolute cap: never work past 5h after the job started, even if the
# per-stage budget would allow more (mirrors Helium's 5.5h cap, tightened
# for our 330-minute job timeout).
MAX_SECONDS_AFTER_JOB_START = 5 * 60 * 60

PHASES = ("bootstrap", "version", "setup", "gn", "build", "done")


class StageError(RuntimeError):
    """An expected stage failure (real error, not a timeout)."""


class BudgetExpired(Exception):
    """The time budget ran out; checkpoint and resume later."""


def log(message, file=sys.stdout):
    print(message, file=file, flush=True)


def run(cmd, cwd, env=None, timeout_at=None, graceful=False):
    """Run a command.

    Returns its exit code. Raises BudgetExpired if timeout_at passes
    before the command finishes.

    graceful=True (ninja on Windows): on timeout, send CTRL_BREAK to the
    child's process group (like Helium's GenerateConsoleCtrlEvent), wait
    up to 120s for a clean stop, then terminate/kill. Ninja stops
    starting new edges and finishes the running ones, so its build
    state stays resumable.
    """
    printable = (
        subprocess.list2cmdline(cmd) if os.name == "nt" else " ".join(cmd)
    )
    log(f"\n[{cwd}] {printable}")
    kwargs = {"cwd": str(cwd)}
    if env is not None:
        kwargs["env"] = env
    if graceful and os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(cmd, **kwargs)
    while True:
        try:
            return proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            if timeout_at is not None and time.time() >= timeout_at:
                log("Time budget expired, stopping the command...")
                if graceful and os.name == "nt":
                    try:
                        ctypes.windll.kernel32.GenerateConsoleCtrlEvent(
                            1, proc.pid
                        )
                    except OSError as error:
                        log(f"Could not send CTRL_BREAK: {error}")
                    try:
                        proc.wait(timeout=120)
                    except subprocess.TimeoutExpired:
                        log("Command did not stop cleanly, terminating...")
                        proc.terminate()
                        try:
                            proc.wait(timeout=60)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait()
                    raise BudgetExpired()
                proc.terminate()
                try:
                    proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                raise BudgetExpired()


def run_checked(cmd, cwd, env=None, timeout_at=None, graceful=False):
    """Run a command, raising StageError on a non-zero exit code."""
    code = run(cmd, cwd, env=env, timeout_at=timeout_at, graceful=graceful)
    if code != 0:
        raise StageError(f"command failed with exit code {code}: {cmd[0]}")
    return code


def set_output(name, value):
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    log(f"::set-output name={name}::{value}")


def read_state(build_root):
    path = build_root / STATE_FILE_NAME
    if not path.is_file():
        return {"phase": "bootstrap"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise StageError(f"could not read stage state {path}: {error}") from error
    if data.get("phase") not in PHASES:
        raise StageError(f"unknown phase in stage state {path}: {data!r}")
    return data


def write_state(build_root, state):
    path = build_root / STATE_FILE_NAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def disk_free_gb(path):
    try:
        return shutil.disk_usage(path).free / (1024 ** 3)
    except OSError:
        return -1.0


def ninja_jobs():
    """Parallel ninja jobs: min(CPUs, free RAM / 3 GiB), at least 1.

    Same heuristic as Helium's stage action (3 GiB per job).
    """
    cpus = os.cpu_count() or 4
    try:
        if os.name == "nt":
            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                free_gb = status.ullAvailPhys / (1024 ** 3)
                return max(1, min(cpus, int(free_gb // 3)))
    except (OSError, AttributeError) as error:
        log(f"Could not read free memory ({error}); using CPU count only.")
    return max(1, cpus)


def find_executable(name):
    path = shutil.which(name)
    if path is None:
        raise StageError(f"required command was not found in PATH: {name}")
    return path


def depot_tool(depot_tools, name):
    """Path to a depot_tools command (handles the Windows .bat suffix)."""
    suffix = ".bat" if os.name == "nt" else ""
    path = depot_tools / f"{name}{suffix}"
    if not path.is_file():
        raise StageError(f"depot_tools command does not exist: {path}")
    return str(path)


def python_script_tool(depot_tools, name):
    """Run a depot_tools tool via its .py companion when it is a .bat.

    Mirrors Thorium build.py's platform_command(): never invoke a .bat
    wrapper without its Python companion.
    """
    bat = depot_tools / f"{name}.bat"
    py = depot_tools / f"{name}.py"
    if os.name == "nt" and bat.is_file() and py.is_file():
        return [sys.executable, str(py)]
    if os.name == "nt" and bat.is_file():
        return [str(bat)]
    tool = depot_tools / name
    if tool.is_file():
        return [str(tool)]
    raise StageError(f"depot_tools tool does not exist: {name}")


def git_clone(url, dest, recursive=False):
    git = find_executable("git")
    cmd = [git, "clone"]
    if recursive:
        cmd.append("--recursive")
    cmd.extend([url, str(dest)])
    run_checked(cmd, dest.parent)


def chromium_checkout_complete(chromium_src, thorium_root):
    """Whether the Chromium checkout looks complete (get_repo.py's test)."""
    parent = chromium_src.parent
    if not (parent / ".gclient").is_file():
        return False
    if not (chromium_src / ".git").exists():
        return False
    nested = []
    try:
        sys.path.insert(0, str(thorium_root))
        from trunk import CHROMIUM_NESTED_CHECKOUTS

        nested = [rel for rel, _ in CHROMIUM_NESTED_CHECKOUTS]
    except (ImportError, AttributeError) as error:
        log(f"Could not import trunk.CHROMIUM_NESTED_CHECKOUTS ({error}); "
            "falling back to a basic checkout check.")
        return (chromium_src / "BUILD.gn").is_file()
    finally:
        if str(thorium_root) in sys.path:
            sys.path.remove(str(thorium_root))
    return all((chromium_src / rel / ".git").exists() for rel in nested)


def depot_tools_initialized(depot_tools):
    return (depot_tools / "python3_bin_reldir.txt").is_file()


def _init_env(env):
    """Env for the depot_tools bootstrap.

    update_depot_tools.bat exits immediately (doing nothing, exit 0)
    when DEPOT_TOOLS_UPDATE=0 — see its line
    `IF "%DEPOT_TOOLS_UPDATE%" == "0" GOTO :EOF` — so the variable
    stage.py sets for the rest of the build must be unset here, or the
    bootstrap silently does nothing and python3_bin_reldir.txt never
    appears (this killed run #4).
    """
    init_env = dict(env)
    init_env.pop("DEPOT_TOOLS_UPDATE", None)
    return init_env


def initialize_depot_tools(depot_tools, env, deadline):
    """Bootstrap depot_tools' bundled Python (gclient/update_depot_tools).

    A fresh git clone of depot_tools is NOT initialized: fetch.bat fails
    with 'python3_bin_reldir.txt not found' until gclient or
    update_depot_tools has run once. Thorium's get_repo.py never does
    this itself — it just clones (or reuses) and runs fetch.bat.
    """
    if depot_tools_initialized(depot_tools):
        log("depot_tools already initialized; skipping bootstrap.")
        return
    log("\nInitializing depot_tools (first run bootstraps its Python)...")
    if os.name == "nt":
        init = depot_tools / "update_depot_tools.bat"
        if not init.is_file():
            raise StageError(f"update_depot_tools.bat not found: {init}")
        cmd = [str(init)]
    else:
        cmd = python_script_tool(depot_tools, "gclient") + ["--version"]
    run_checked(cmd, depot_tools, env=_init_env(env), timeout_at=deadline)
    if not depot_tools_initialized(depot_tools):
        raise StageError(
            "depot_tools still not initialized after bootstrap "
            "(python3_bin_reldir.txt missing)"
        )
    log("depot_tools initialized.")


# Directories that may be deleted to free disk on a GitHub-hosted
# Windows runner. EXACT paths only — never environment variables, never
# a drive root, never anything else. A previous version built the list
# from env vars and one resolved to C:\ itself, deleting Git and the
# runner's node.exe and killing run #3. The allowlist + _safe_to_delete
# guard below exist so that can never happen again.
DELETABLE_TOOLCHAINS = (
    r"C:\Android\android-sdk",
    r"C:\ghcup",
    r"C:\Program Files\Java",
    r"C:\Program Files\Haskell Platform",
)
DELETABLE_JDK_ROOT = r"C:\hostedtoolcache\windows\Java_Temurin-Hotspot_jdk"


def _safe_to_delete(path):
    r"""True only for paths inside the deletable-toolchain allowlist.

    Deliberately strict: a drive root, a top-level directory, and
    anything outside the allowlist is refused — including Git
    (C:\Program Files\Git), the runner (C:\actions-runner), the
    Windows SDK, Python, the build tree, and the repo checkout.
    """
    p = PureWindowsPath(path)
    for base in DELETABLE_TOOLCHAINS:
        b = PureWindowsPath(base)
        if p == b or p.is_relative_to(b):
            return True
    jdk_root = PureWindowsPath(DELETABLE_JDK_ROOT)
    if p != jdk_root and p.is_relative_to(jdk_root):
        return True
    return False


def free_disk_space(build_root):
    """Remove large preinstalled toolchains the Chromium build does not need.

    GitHub-hosted Windows runners start with only ~28 GiB free, but a
    Chromium checkout + build needs far more. Only the allowlisted
    toolchain dirs (Android SDK, Temurin JDKs, Haskell, Java) are ever
    removed; _safe_to_delete refuses everything else. Nothing the build
    uses (VS toolchain, Windows SDK, Python, Node, git, 7z) is touched.
    """
    if os.name != "nt":
        return
    before = disk_free_gb(build_root)
    candidates = [Path(p) for p in DELETABLE_TOOLCHAINS]
    jdk_root = Path(DELETABLE_JDK_ROOT)
    if jdk_root.is_dir():
        candidates.extend(p for p in jdk_root.iterdir() if p.is_dir())
    seen = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        if not path.is_dir():
            continue
        if not _safe_to_delete(path):
            log(f"Refusing to delete (not in allowlist): {path}")
            continue
        log(f"Removing unused preinstalled toolchain: {path}")
        shutil.rmtree(path, ignore_errors=True)
    after = disk_free_gb(build_root)
    if after >= 0 and before >= 0:
        log(f"Free disk: {before:.0f} GiB -> {after:.0f} GiB after cleanup.")


def phase_bootstrap(args, ctx, deadline):
    """Clone depot_tools + Thorium, fetch Chromium (get_repo.py)."""
    build_root = ctx["build_root"]
    chromium_src = ctx["chromium_src"]
    thorium_root = ctx["thorium_root"]
    depot_tools = ctx["depot_tools"]
    env = ctx["env"]

    if not depot_tools.is_dir():
        log(f"\nCloning depot_tools into {depot_tools}")
        git_clone(DEPOT_TOOLS_REPO, depot_tools)
    else:
        log(f"\nUsing existing depot_tools: {depot_tools}")
    # A fresh clone is not initialized: fetch.bat needs the bundled
    # Python (python3_bin_reldir.txt), which only appears after gclient
    # / update_depot_tools runs once. get_repo.py does not do this.
    initialize_depot_tools(depot_tools, env, deadline)

    if not thorium_root.is_dir():
        log(f"\nCloning Thorium into {thorium_root}")
        git_clone(THORIUM_REPO, thorium_root, recursive=True)
        git = find_executable("git")
        run_checked(
            [git, "checkout", args.thorium_commit], thorium_root, env=env
        )
        run_checked(
            [git, "submodule", "update", "--init", "--recursive"],
            thorium_root,
            env=env,
        )
    else:
        log(f"\nUsing existing Thorium checkout: {thorium_root}")

    fetch_marker = chromium_src.parent / FETCH_MARKER
    if chromium_checkout_complete(chromium_src, thorium_root):
        if fetch_marker.is_file():
            log("Fetch marker present; completing the interrupted fetch "
                "with get_repo.py --recover-incomplete.")
            run_checked(
                [
                    sys.executable,
                    str(thorium_root / "get_repo.py"),
                    "--yes",
                    "--recover-incomplete",
                    f"--chromium-src={chromium_src}",
                    f"--thorium-root={thorium_root}",
                    f"--depot-tools={depot_tools}",
                ],
                thorium_root,
                env=env,
                timeout_at=deadline,
            )
        else:
            log("Chromium checkout already complete; skipping fetch.")
    else:
        if fetch_marker.is_file():
            log("Incomplete Chromium checkout; recovering with "
                "get_repo.py --recover-incomplete.")
            run_checked(
                [
                    sys.executable,
                    str(thorium_root / "get_repo.py"),
                    "--yes",
                    "--recover-incomplete",
                    f"--chromium-src={chromium_src}",
                    f"--thorium-root={thorium_root}",
                    f"--depot-tools={depot_tools}",
                ],
                thorium_root,
                env=env,
                timeout_at=deadline,
            )
        else:
            log("Fetching Chromium (this downloads tens of GB and can "
                "take hours; it is resumed by the next stage if the "
                "budget runs out).")
            # NOTE: no --no-history: version.py needs the full tag
            # history to check out tags/156.0.8078.80.
            run_checked(
                [
                    sys.executable,
                    str(thorium_root / "get_repo.py"),
                    "--yes",
                    f"--chromium-src={chromium_src}",
                    f"--thorium-root={thorium_root}",
                    f"--depot-tools={depot_tools}",
                ],
                thorium_root,
                env=env,
                timeout_at=deadline,
            )
    log("\nBootstrap phase complete.")


def phase_version(args, ctx, deadline):
    """Thorium's version.py: pin Chromium to the Thorium tag."""
    log(f"\nChecking out Chromium tag {args.chromium_tag} (version.py).")
    run_checked(
        [
            sys.executable,
            str(ctx["thorium_root"] / "version.py"),
            f"--chromium-src={ctx['chromium_src']}",
            f"--depot-tools={ctx['depot_tools']}",
        ],
        ctx["thorium_root"],
        env=ctx["env"],
        timeout_at=deadline,
    )
    log("\nVersion phase complete.")


def phase_setup(args, ctx, deadline):
    """Thorium's setup.py: overlays + patch series + GRD rebase."""
    log(f"\nApplying Thorium setup for profile --{args.profile} "
        "(overlays, patch series, GRD rebase).")
    run_checked(
        [
            sys.executable,
            str(ctx["thorium_root"] / "setup.py"),
            f"--{args.profile}",
            f"--chromium-src={ctx['chromium_src']}",
            f"--thorium-root={ctx['thorium_root']}",
        ],
        ctx["thorium_root"],
        env=ctx["env"],
        timeout_at=deadline,
    )
    log("\nSetup phase complete.")


def write_args_gn(args, ctx):
    """Write out/thorium/args.gn from args.gn.example + Secrets.

    The three Google API key lines are replaced with the values from
    the environment (GitHub Secrets). When a secret is unset or empty,
    the line stays an empty string. Key values are never written to
    any file except this generated args.gn inside the build tree, and
    never printed to the log.
    """
    template = ctx["repo_root"] / "args.gn.example"
    if not template.is_file():
        raise StageError(f"args.gn template not found: {template}")
    out_dir = ctx["chromium_src"] / OUT_DIR_NAME
    out_dir.mkdir(parents=True, exist_ok=True)
    text = template.read_text(encoding="utf-8")

    keys = {
        "google_api_key": os.environ.get("GOOGLE_API_KEY", ""),
        "google_default_client_id": os.environ.get(
            "GOOGLE_DEFAULT_CLIENT_ID", ""
        ),
        "google_default_client_secret": os.environ.get(
            "GOOGLE_DEFAULT_CLIENT_SECRET", ""
        ),
    }
    for name, value in keys.items():
        pattern = re.compile(
            rf'^{re.escape(name)}\s*=\s*".*"$', re.MULTILINE
        )
        replacement = f'{name} = "{value}"'
        new_text, count = pattern.subn(replacement, text)
        if count != 1:
            raise StageError(
                f"expected exactly one {name} line in {template}, "
                f"found {count}; template changed?"
            )
        text = new_text
    args_gn = out_dir / "args.gn"
    args_gn.write_text(text, encoding="utf-8")
    set_keys = [name for name, value in keys.items() if value]
    if set_keys:
        log(f"args.gn written; API keys injected for: "
            f"{', '.join(set_keys)} (values not shown).")
    else:
        log("args.gn written; no Google API keys set "
            "(Sync will be unavailable, the browser still builds).")
    return args_gn


def phase_gn(args, ctx, deadline):
    """Generate the Ninja build files with `gn gen`."""
    write_args_gn(args, ctx)
    chromium_src = ctx["chromium_src"]
    out_dir = chromium_src / OUT_DIR_NAME
    gn_exe = chromium_src / "buildtools" / "win" / "gn.exe"
    if gn_exe.is_file():
        gn_cmd = [str(gn_exe)]
    else:
        # Fall back to depot_tools' gn (downloaded by runhooks).
        gn_cmd = python_script_tool(ctx["depot_tools"], "gn")
    log(f"\nRunning gn gen {OUT_DIR_NAME} (fail on unused args).")
    run_checked(
        [*gn_cmd, "gen", OUT_DIR_NAME, "--fail-on-unused-args"],
        chromium_src,
        env=ctx["env"],
        timeout_at=deadline,
    )
    if not (out_dir / "build.ninja").is_file():
        raise StageError(f"gn gen did not produce {out_dir}/build.ninja")
    log("\nGN phase complete.")


def phase_build(args, ctx, deadline):
    """Run autoninja on Thorium's Windows targets.

    Phases mirror Thorium build.py's default Windows policy:
    thorium_all, then thorium_installer.
    """
    chromium_src = ctx["chromium_src"]
    out_dir = chromium_src / OUT_DIR_NAME
    autoninja = ctx["depot_tools"] / "autoninja.py"
    if not autoninja.is_file():
        raise StageError(f"autoninja.py not found in depot_tools: {autoninja}")
    jobs = ninja_jobs()
    log(f"\nBuilding with autoninja -j {jobs}: "
        f"thorium_all, thorium_installer")
    env = dict(ctx["env"])
    env["NINJA_SUMMARIZE_BUILD"] = "1"
    env["NINJA_STATUS"] = "[%r processes, %f/%t @ %o/s | %e sec. ] "
    code = run(
        [
            sys.executable,
            str(autoninja),
            "-C",
            str(out_dir),
            "-j",
            str(jobs),
            "thorium_all",
            "thorium_installer",
        ],
        chromium_src,
        env=env,
        timeout_at=deadline,
        graceful=True,
    )
    if code != 0:
        raise StageError(
            f"autoninja failed with exit code {code}; fix the build "
            f"error before re-running (the next stage resumes from the "
            f"same tree, it does not start over)."
        )
    exe = out_dir / "thorium.exe"
    if not exe.is_file():
        raise StageError(
            f"autoninja reported success but {exe} is missing; "
            f"the Thorium patches may have changed the output name."
        )
    log(f"\nBuild finished: {exe} exists "
        f"({exe.stat().st_size / (1024 ** 2):.1f} MiB).")


def checkpoint(args, ctx):
    """Zip the whole build root for the next stage (Helium-style).

    The zip contains the build-root directory itself (lumo-build/...),
    so the next stage extracts it with `7z x build-state.zip -oC:\\`.
    """
    build_root = ctx["build_root"]
    zip_path = Path(args.state_zip)
    if zip_path.is_file():
        zip_path.unlink()
    seven_zip = find_executable("7z")
    log(f"\nCheckpointing build root to {zip_path} (7z, -mx=3; "
        f"this can take a while for a tens-of-GB tree)...")
    run_checked(
        [
            seven_zip,
            "a",
            "-tzip",
            str(zip_path),
            build_root.name,
            "-mx=3",
            "-mtc=on",
        ],
        build_root.parent,
        env=ctx["env"],
    )
    size_gb = zip_path.stat().st_size / (1024 ** 3)
    log(f"Checkpoint written: {zip_path} ({size_gb:.1f} GiB).")
    if size_gb > 9:
        log("WARNING: the checkpoint is over ~9 GiB; GitHub Artifacts "
            "may refuse to upload it. If the upload fails, the build "
            "needs sccache or a different checkpoint strategy (see "
            "PLAN.md).")


def main():
    parser = argparse.ArgumentParser(
        description="Lumo single build stage (Helium-style resume)"
    )
    parser.add_argument(
        "--build-root",
        default=r"C:/lumo-build",
        help="Root of the Chromium build tree (src + .gclient + state); "
        "Thorium and depot_tools live in sibling dirs",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Lumo repo checkout (default: $GITHUB_WORKSPACE or cwd)",
    )
    parser.add_argument(
        "--state-zip",
        default="build-state.zip",
        help="Where to write the checkpoint zip (default: ./build-state.zip)",
    )
    parser.add_argument(
        "--budget-seconds",
        type=int,
        default=DEFAULT_BUDGET_SECONDS,
        help="Work budget for this stage in seconds",
    )
    parser.add_argument(
        "--job-started-at",
        type=int,
        default=0,
        help="Unix epoch when the Actions job started (0 = now)",
    )
    parser.add_argument(
        "--profile",
        default="avx2",
        choices=("avx2", "sse3"),
        help="Thorium SIMD profile (Lumo builds AVX2 only)",
    )
    parser.add_argument(
        "--thorium-commit",
        default=DEFAULT_THORIUM_COMMIT,
        help="Pinned Thorium commit to build from",
    )
    parser.add_argument(
        "--chromium-tag",
        default=DEFAULT_CHROMIUM_TAG,
        help="Pinned Chromium tag (from Thorium's version.py)",
    )
    args = parser.parse_args()

    started = time.time()
    job_started = args.job_started_at or int(started)
    # Stop working in time to zip + upload the checkpoint inside the
    # 330-minute job timeout.
    deadline = min(
        started + args.budget_seconds,
        job_started + MAX_SECONDS_AFTER_JOB_START,
    )
    if deadline <= started + 60:
        print("error: no usable time budget left in this job", file=sys.stderr)
        return 2

    build_root = Path(args.build_root).expanduser()
    build_root.mkdir(parents=True, exist_ok=True)
    repo_root = Path(
        args.repo_root or os.environ.get("GITHUB_WORKSPACE", os.getcwd())
    )
    # Layout (must not overlap — Thorium's get_repo.py treats the
    # Chromium root as the parent of the src dir and refuses any
    # checkout path that contains another one):
    #   Chromium root: C:/lumo-build        (src + .gclient + state)
    #   Thorium:       C:/thorium           (sibling, re-cloned per stage)
    #   depot_tools:   C:/depot_tools       (sibling, re-cloned per stage)
    # The checkpoint only covers the build root; bootstrap re-clones
    # Thorium/depot_tools when they are missing.
    depot_tools = build_root.parent / "depot_tools"
    thorium_root = build_root.parent / "thorium"
    chromium_src = build_root / "src"

    env = os.environ.copy()
    env["PATH"] = str(depot_tools) + os.pathsep + env.get("PATH", "")
    env["DEPOT_TOOLS_DIR"] = str(depot_tools)
    # Do not let depot_tools self-update mid-build (Helium uses the
    # runner's VS toolchain instead of a downloaded one).
    env["DEPOT_TOOLS_UPDATE"] = "0"
    env["DEPOT_TOOLS_WIN_TOOLCHAIN"] = "0"

    ctx = {
        "build_root": build_root,
        "repo_root": repo_root,
        "depot_tools": depot_tools,
        "thorium_root": thorium_root,
        "chromium_src": chromium_src,
        "env": env,
    }

    # Fail fast on overlapping checkout paths instead of letting
    # get_repo.py reject them after cloning (first-run failure).
    named_paths = (
        ("Chromium root", chromium_src.parent),
        ("Thorium", thorium_root),
        ("depot_tools", depot_tools),
    )
    for i, (left_name, left_path) in enumerate(named_paths):
        for right_name, right_path in named_paths[i + 1:]:
            if left_path.is_relative_to(right_path) or right_path.is_relative_to(
                left_path
            ):
                raise StageError(
                    f"checkout paths must not overlap: {left_name}={left_path}, "
                    f"{right_name}={right_path}"
                )

    log(f"Lumo build stage: build root {build_root}")
    log(f"Thorium commit: {args.thorium_commit}")
    log(f"Chromium tag:    {args.chromium_tag}")
    log(f"Profile:         {args.profile} (AVX2 only)")
    log(f"Time budget:     {(deadline - started) / 60:.0f} minutes")
    free_gb = disk_free_gb(build_root)
    if free_gb >= 0:
        log(f"Free disk:       {free_gb:.0f} GiB")
        if free_gb < 100:
            log("WARNING: less than 100 GiB free; a Chromium checkout "
                "plus build outputs may not fit (see PLAN.md). "
                "Removing unused preinstalled toolchains to make room.")
            free_disk_space(build_root)
    if os.name == "nt":
        # The child ninja process gets CTRL_BREAK on timeout; make sure
        # this parent ignores console Ctrl-C so it survives to
        # checkpoint. (GenerateConsoleCtrlEvent targets the child's
        # process group, but be safe.)
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        except (OSError, ValueError):
            pass

    state = read_state(build_root)
    log(f"Resuming from phase: {state['phase']}")

    phase_fns = {
        "bootstrap": phase_bootstrap,
        "version": phase_version,
        "setup": phase_setup,
        "gn": phase_gn,
        "build": phase_build,
    }
    try:
        index = PHASES.index(state["phase"])
        for phase in PHASES[index:-1]:
            phase_fns[phase](args, ctx, deadline)
            state = {"phase": PHASES[PHASES.index(phase) + 1]}
            write_state(build_root, state)
            log(f"Phase {phase} done; next phase: {state['phase']}")
    except BudgetExpired:
        write_state(build_root, state)
        log("\nBudget expired: checkpointing and exiting 42 "
            "(resume in the next stage).")
        set_output("finished", "false")
        try:
            checkpoint(args, ctx)
        except StageError as error:
            log(f"error: checkpoint failed: {error}", file=sys.stderr)
            return EXIT_FAILURE
        return EXIT_INCOMPLETE
    except StageError as error:
        log(f"error: {error}", file=sys.stderr)
        return EXIT_FAILURE
    except KeyboardInterrupt:
        log("error: build interrupted", file=sys.stderr)
        return 130

    write_state(build_root, {"phase": "done"})
    set_output("finished", "true")
    log("\nStage complete: build finished.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
