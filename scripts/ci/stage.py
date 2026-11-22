#!/usr/bin/env python3
"""Lumo CI stage script.

Runs ONE build stage inside a single GitHub Actions job, Helium-style.

Pipeline (all inside the job's time budget):
  1. bootstrap -- clone depot_tools + Thorium (pinned commit), then
     download the prepared Chromium tarballs (base + win-x64) from
     imputnet's chromium-tarballs release, verify their pinned
     SHA-256 hashes, and unpack them into the src tree. This is
     Helium's x64 acquisition method; runs #5-#7 all died in build-1
     because even a shallow gclient fetch + sync (~45-55 GiB) does
     not fit the runner's disk, while the two tarballs are ~3.5 GiB
     compressed and ~15 GiB unpacked. The tarball tree is already
     gclient-synced and post-runhooks (it contains
     build/util/LASTCHANGE, buildtools/win/gn.exe, clang-cl.exe),
     so no fetch, sync, or runhooks step exists anymore.
  2. version   -- verify chrome/VERSION in the tarball tree equals
     the pinned tag (156.0.8078.80). The tarballs are published per
     exact version, so there is nothing to check out.
  3. setup     -- Thorium's setup.py --avx2: copy overlays, apply the
     Thorium patch series, GRD/XTB rebase. Lumo's series overrides
     (audit corrections) run just before setup.py and Lumo's
     branding (patches, icon, strings) just after it; see the
     "Lumo series overrides and branding" section below.
  4. gn        -- write out/thorium/args.gn from args.gn.example, injecting
     the Google API keys from the environment (Secrets), then `gn gen`.
  5. build     -- autoninja thorium_all + thorium_installer with a time
     budget. The Thorium patches rename the binary to thorium.exe; the
     rename to lumo.exe happens at packaging time (build-final job).
     Profile is AVX2 only (target machine: Intel Core i5-8350U).

Resume model (mirrors Helium's stage action):
  * The whole build root (C:/lumo-build: Chromium src + the tarball
    marker + lumo-stage-state.json) is zipped with 7z and uploaded
    as an artifact when the time budget expires. The next stage
    downloads and extracts it and continues from the phase recorded
    in lumo-stage-state.json. The downloaded tarball archives are
    deleted as soon as their tree is unpacked, and the download
    directory is excluded from the checkpoint zip, so an interrupted
    download is simply repeated by the next stage.
    Thorium (C:/thorium) and depot_tools (C:/depot_tools) live OUTSIDE
    the build root as siblings: Thorium's tooling treats the Chromium
    root as the parent of the src dir and refuses overlapping
    checkout paths, so thorium/depot_tools must not sit under
    C:/lumo-build. They are re-cloned when missing — by the
    bootstrap phase, or by the ensure step in main() when a stage
    resumes past bootstrap (run #11's build-2 resumed into the
    build phase with no depot_tools and died immediately).
  * Exit code 42 means "build incomplete, resume in the next stage"
    (Helium's convention). It is NOT a failure.
  * Exit code 0 means the build finished; thorium.exe was verified to
    exist. The state artifact is still uploaded so build-final can
    download the finished tree.
  * Any other non-zero exit code is a real failure and fails the job.

Status after run #12 (2026-11-15):
  * The full build COMPLETED in CI: run #12's build-4 finished the
    compile and checkpointed the finished tree (lumo-build-state-4,
    9.19 GiB). The tarball acquisition, setup, gn gen and the stage
    chain all ran end to end. build-final (Package Lumo) then
    failed on DISK SPACE — it extracted the whole ~20.7 GiB tree
    next to the ~10 GB checkpoint zip — and was rewritten to
    extract only out/thorium and prune the build-only files (see
    build-windows.yml). The rewritten build-final has not run in
    CI yet.
  * Renaming thorium.exe to lumo.exe at packaging time is VERIFIED:
    a package salvaged from the run #12 checkpoint with that rename
    was launched by seasea1010 on Windows (2026-11-15;
    chrome://version showed lumo.exe, 156.0.8078.80).
  * The build-state zip is large (5.9 GiB at stage 1, growing to
    9.19 GiB for the finished tree in run #12). Zipping and
    uploading it must fit the reserve after the work deadline —
    see MAX_SECONDS_AFTER_JOB_START.
  * The tarball tree has no .git and no .gclient. Thorium's
    setup.py only checks that src/.git exists and its patch series
    runs `git apply` inside the tree, so bootstrap creates a bare
    `git init` repo there.
  * Thorium's patch series modifies ~51 files the tarballs do not
    ship (chrome/android/java/** and friends, which only an
    Android checkout carries). phase_setup fetches those from the
    official Chromium mirrors at the pinned tag, and a series
    filter drops any entry whose targets are still absent (a
    safety net). Locally validated 2026-11-13 against the pinned
    Thorium commit: fetch 51/51, filter dropped nothing, and
    setup.py completed end to end on the tarball tree.
  * Thorium's own branding (About page, installer product name,
    user-data folder name) still says Thorium, so the packaged
    browser shares an installed Thorium's profile. Full Lumo
    rebranding (product name, user-data dir, icon, About credits)
    is the build #13 work (see PLAN.md).
"""

import argparse
import base64
import binascii
import ctypes
import hashlib
import io
import ast
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PureWindowsPath

# Helium's convention: exit code 42 means "build incomplete, resume later".
EXIT_INCOMPLETE = 42
EXIT_FAILURE = 1
# Exit code 44: the disk watchdog aborted the stage before the disk
# filled completely (see _disk_watchdog). A real failure for the job,
# but one that leaves the runner — and this log — alive.
EXIT_DISK_ABORT = 44

# Disk watchdog tuning. Run #6 filled the disk so completely that the
# Actions runner itself crashed writing its own diagnostic log, and
# GitHub kept no job log at all: whether the cleanup freed anything,
# and where the disk went, is unknowable for that run. The watchdog
# polls free space and aborts while DISK_ABORT_FREE_GB still remain,
# so the job ends normally and the log survives with the drain curve
# and the largest directories recorded.
DISK_WATCH_INTERVAL_SECONDS = 30
DISK_ABORT_FREE_GB = 3.0

# Set by main(): log() also appends to this file (build root
# stage.log, which rides along in the checkpoint zip), and the
# watchdog reads the current phase from _CURRENT_PHASE.
_LOG_FILE = None
_CURRENT_PHASE = {"phase": "startup"}

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

# ---------------------------------------------------------------------------
# Tarball acquisition (run #8).
#
# Helium's x64 builds never gclient-clone: they unpack the prepared
# Chromium tarballs published by imputnet (Helium's downloads.x64.ini
# points at the same source). Two archives make up the full tree:
#   * base    -- the Chromium source tree (DEPS, chrome/VERSION, ...)
#   * win-x64 -- the Windows DEPS checkouts and runhooks outputs
#     (third_party/*, buildtools/win/gn.exe, build/util/LASTCHANGE,
#     clang-cl.exe, the rust toolchain, PGO profiles)
# Sizes and SHA-256 hashes below were verified locally on 2026-11-13
# against the release's official SHA256SUMS, and both tar listings
# were read end to end (587,942 + 78,486 entries). The workflow still
# downloads from the official sources and re-verifies the hashes.
# ---------------------------------------------------------------------------
TARBALL_URL_TEMPLATES = (
    "https://tarballs.imput.net/chromium-{tag}/{filename}",
    "https://github.com/imputnet/chromium-tarballs/releases/download/"
    "chromium-{tag}/{filename}",
)
TARBALLS = (
    {
        "name": "base",
        "filename": "chromium-{tag}-base.tar.zst",
        "size": 1_983_332_802,
        "sha256": "0d3ab019bebfa7d029004a2612839e6ef5cbb1850cee4ba86987c5c25594bf33",
        "sentinels": ("BUILD.gn", "DEPS", "chrome/VERSION"),
    },
    {
        "name": "win-x64",
        "filename": "chromium-{tag}-win-x64.tar.zst",
        "size": 1_731_634_454,
        "sha256": "68e23fac830e31542ae9f017e98c7535ad43cd64796abec774ec0e75d0c3cac3",
        "sentinels": (
            "buildtools/win/gn.exe",
            "build/util/LASTCHANGE",
            "third_party/llvm-build/Release+Asserts/bin/clang-cl.exe",
        ),
    },
)
TARBALL_DIR_NAME = "lumo-tarballs"
TARBALL_MARKER = ".lumo-tarball-tree.json"

# Default work budget per job: 4h45m. The job timeout is 330 minutes
# (5.5h); the remaining ~45 minutes are reserved for zipping the build
# state and uploading the artifact. Tunable via --budget-seconds.
DEFAULT_BUDGET_SECONDS = 4 * 60 * 60 + 45 * 60
# Absolute cap: never work past 4h45m after the job started, even if
# the per-stage budget would allow more. Together with the job
# timeout below this guarantees at least ~45 minutes for zipping
# the checkpoint and uploading the artifact. Run #12's zip + upload
# took ~10-15 minutes at 6-9 GiB, but the zip grows with the tree,
# and a checkpoint lost to the job timeout loses the whole stage —
# the reserve is worth more than the last compile minutes before
# the cap (audit before run #13; previously 5h, a ~30 minute
# worst-case reserve).
MAX_SECONDS_AFTER_JOB_START = 4 * 60 * 60 + 45 * 60
# The Actions job timeout (build-stage.yml: timeout-minutes: 330).
# Used only to log the checkpoint/upload reserve; keep in sync.
JOB_TIMEOUT_SECONDS = 330 * 60

PHASES = ("bootstrap", "version", "setup", "gn", "build", "done")


class StageError(RuntimeError):
    """An expected stage failure (real error, not a timeout)."""


class BudgetExpired(Exception):
    """The time budget ran out; checkpoint and resume later."""


def log(message, file=sys.stdout):
    print(message, file=file, flush=True)
    if file is sys.stdout and _LOG_FILE is not None:
        try:
            _LOG_FILE.write(str(message) + "\n")
            _LOG_FILE.flush()
        except (OSError, ValueError):
            pass


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
    # The legacy "::set-output" workflow command used to be echoed
    # here as a fallback; GitHub deprecated it (it now raises a
    # deprecation annotation on every run), and the GITHUB_OUTPUT
    # file above is the supported channel, so it was removed.


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


def _largest_subdirs(root, limit=8):
    """Best-effort (size GiB, name) of the largest entries under root.

    Bounded walk (file-count cap) so the watchdog's dying message does
    not itself take forever on a tens-of-GB tree.
    """
    sizes = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return sizes
    for entry in entries:
        total = 0
        try:
            if entry.is_file(follow_symlinks=False):
                total = entry.stat(follow_symlinks=False).st_size
            elif entry.is_dir(follow_symlinks=False):
                count = 0
                capped = False
                for dirpath, _dirnames, filenames in os.walk(entry.path):
                    for name in filenames:
                        try:
                            total += os.path.getsize(
                                os.path.join(dirpath, name)
                            )
                        except OSError:
                            pass
                        count += 1
                        if count > 300000:
                            capped = True
                            break
                    if capped:
                        break
        except OSError:
            continue
        sizes.append((total / (1024 ** 3), entry.name))
    sizes.sort(reverse=True)
    return sizes[:limit]


def _disk_watchdog(build_root):
    """Log the free-disk drain; abort before the disk hits zero.

    GitHub only keeps a job's log when the job ends with the runner
    alive. Run #6's runner died of a full disk and the log was lost,
    so this thread trades a doomed build for a diagnosable one: at
    DISK_ABORT_FREE_GB it records the phase and the largest build-root
    directories, then exits with EXIT_DISK_ABORT while the runner can
    still upload the log.
    """
    last_logged = None
    while True:
        time.sleep(DISK_WATCH_INTERVAL_SECONDS)
        free = disk_free_gb(build_root)
        if free < 0:
            continue
        phase = _CURRENT_PHASE["phase"]
        if last_logged is None or abs(free - last_logged) >= 0.5:
            log(f"[disk-watch] free {free:.1f} GiB (phase: {phase})")
            last_logged = free
        if free < DISK_ABORT_FREE_GB:
            log(
                f"error: free disk dropped to {free:.1f} GiB "
                f"(phase: {phase}); aborting before the disk fills "
                "completely so the runner survives and this log is "
                "preserved (run #6 lost its log to a runner crash)."
            )
            log("Largest directories under the build root:")
            for size_gb, name in _largest_subdirs(build_root):
                log(f"  {size_gb:6.1f} GiB  {name}")
            sys.stdout.flush()
            os._exit(EXIT_DISK_ABORT)


def start_disk_watchdog(build_root):
    thread = threading.Thread(
        target=_disk_watchdog,
        args=(build_root,),
        daemon=True,
        name="disk-watchdog",
    )
    thread.start()
    return thread


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


def _partial_clone(url, dest, commit=None, env=None):
    """Blobless partial clone of `url` into `dest`; True on success.

    --filter=blob:none downloads every commit and tree but no file
    blobs; the checkout then lazily fetches exactly one tree's
    blobs, so a pinned checkout costs roughly one tree instead of
    the repo's whole history of blobs (Thorium's history is
    several GiB of them). Unlike a --depth clone, the history is
    complete, so tooling that walks it (depot_tools' bootstrap,
    Thorium's setup scripts) keeps working — a missing blob is
    fetched on demand. On any failure the partial checkout is
    removed and False returned, so callers can fall back to the
    plain full clone this replaced; the fallback keeps a server
    or git version without filter support from becoming a new
    failure mode.
    """
    git = find_executable("git")
    try:
        run_checked(
            [git, "clone", "--filter=blob:none", "--no-checkout",
             url, str(dest)],
            dest.parent,
        )
        run_checked(
            [git, "checkout", commit or "HEAD"], dest, env=env
        )
        return True
    except StageError:
        shutil.rmtree(dest, ignore_errors=True)
        return False


def tarball_tree_complete(build_root, chromium_src, tag):
    """Whether the unpacked tarball tree looks complete.

    The marker is written only after both archives were downloaded,
    hash-verified, and unpacked; the sentinel files (the ones the
    later phases actually consume) are checked on top of it.
    """
    marker = build_root / TARBALL_MARKER
    if not marker.is_file():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if data.get("tag") != tag:
        return False
    return all(
        (chromium_src / sentinel).exists()
        for spec in TARBALLS
        for sentinel in spec["sentinels"]
    )


def _sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_file(url, dest, deadline):
    request = urllib.request.Request(
        url, headers={"User-Agent": "lumo-ci-stage"}
    )
    with urllib.request.urlopen(request) as response, open(
        dest, "wb"
    ) as out:
        downloaded = 0
        last_log = time.time()
        while True:
            if deadline is not None and time.time() >= deadline:
                raise BudgetExpired()
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            downloaded += len(chunk)
            if time.time() - last_log >= 30:
                log(f"  ... {downloaded / (1024 ** 3):.2f} GiB downloaded")
                last_log = time.time()


def download_tarball(spec, tag, dest_dir, deadline):
    """Download one tarball (or reuse a verified copy); return its path.

    The pinned size + SHA-256 are verified for every copy, downloaded
    or reused. A failed mirror falls through to the next one; a size
    or hash mismatch deletes the file so a partial/corrupt download
    is never unpacked or resumed.
    """
    filename = spec["filename"].format(tag=tag)
    dest = dest_dir / filename
    if dest.is_file() and dest.stat().st_size == spec["size"]:
        log(f"Verifying existing download: {filename}")
        if _sha256_of(dest) == spec["sha256"]:
            log("Existing file verified by SHA-256; skipping download.")
            return dest
        log("Existing file failed SHA-256; downloading it again.")
        dest.unlink()
    errors = []
    for template in TARBALL_URL_TEMPLATES:
        url = template.format(tag=tag, filename=filename)
        log(f"\nDownloading {filename}\n  from {url}")
        try:
            _download_file(url, dest, deadline)
        except (OSError, urllib.error.URLError) as error:
            errors.append(f"{url}: {error}")
            log(f"Download failed: {error}")
            continue
        if dest.stat().st_size != spec["size"]:
            errors.append(
                f"{url}: size {dest.stat().st_size} != {spec['size']}"
            )
            log("Downloaded size does not match the pinned size; "
                "trying the next mirror.")
            dest.unlink(missing_ok=True)
            continue
        if _sha256_of(dest) != spec["sha256"]:
            errors.append(f"{url}: SHA-256 mismatch")
            log("Downloaded file failed SHA-256; trying the next mirror.")
            dest.unlink(missing_ok=True)
            continue
        log(f"Downloaded {filename} "
            f"({spec['size'] / (1024 ** 3):.2f} GiB), SHA-256 verified.")
        return dest
    raise StageError(
        f"could not download {filename}: " + "; ".join(errors)
    )


def _extract_with_bsdtar(archive, dest, deadline):
    """Unpack a .tar.zst with Windows' bsdtar, stripping the top dir.

    libarchive in Windows Server 2025 / Windows 11 understands zstd.
    Returns True when tar.exe ran and exited 0; the caller verifies
    the sentinel files either way, because a benign warning (e.g. a
    symlink it could not create) can also produce a non-zero exit.
    """
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    tar_exe = Path(system_root) / "System32" / "tar.exe"
    if not tar_exe.is_file():
        return False
    code = run(
        [
            str(tar_exe),
            "-xf",
            str(archive),
            "-C",
            str(dest),
            "--strip-components",
            "1",
        ],
        dest,
        timeout_at=deadline,
    )
    return code == 0


def _run_7z_pipe(seven_zip, archive, staging, deadline):
    """Stream-decompress with 7z: .tar.zst -> tar -> unpacked files.

    Piping avoids writing the ~9 GiB intermediate .tar to disk.
    Returns True only when both 7z processes exit 0.
    """
    log(f"\n[{staging}] 7z x -so {archive.name} | "
        "7z x -si -ttar (streaming unpack)")
    producer = subprocess.Popen(
        [seven_zip, "x", "-so", str(archive)],
        stdout=subprocess.PIPE,
    )
    consumer = subprocess.Popen(
        [seven_zip, "x", "-si", "-ttar", f"-o{staging}", "-y"],
        stdin=producer.stdout,
    )
    producer.stdout.close()
    while True:
        code = consumer.poll()
        if code is not None:
            producer.wait()
            return code == 0 and producer.returncode == 0
        if deadline is not None and time.time() >= deadline:
            consumer.kill()
            producer.kill()
            consumer.wait()
            producer.wait()
            raise BudgetExpired()
        time.sleep(5)


def _extract_with_7z(archive, dest, top_name, staging, deadline):
    """Fallback unpack via 7z streaming + robocopy move.

    7z cannot strip the leading chromium-<tag> directory, so the
    archive is unpacked into a staging dir and the top directory's
    contents are moved into dest (same-volume renames via robocopy).
    """
    try:
        seven_zip = find_executable("7z")
    except StageError:
        return False
    if staging.is_dir():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    if not _run_7z_pipe(seven_zip, archive, staging, deadline):
        return False
    top = staging / top_name
    if not top.is_dir():
        return False
    robocopy = shutil.which("robocopy") or "robocopy"
    code = run(
        [
            robocopy,
            str(top),
            str(dest),
            "/E",
            "/MOVE",
            "/MT:32",
            "/R:1",
            "/W:1",
            "/NFL",
            "/NDL",
            "/NJH",
            "/NJS",
        ],
        staging,
        timeout_at=deadline,
    )
    shutil.rmtree(staging, ignore_errors=True)
    # robocopy exit codes 0-7 are success variants; >= 8 is failure.
    return code < 8


def extract_tarball(archive, spec, tag, chromium_src, download_dir, deadline):
    """Unpack one tarball into the src tree and verify its sentinels."""
    sentinels = spec["sentinels"]
    if all((chromium_src / s).exists() for s in sentinels):
        log(f"The {spec['name']} tree is already unpacked "
            "(sentinel files present); skipping unpack.")
        return
    chromium_src.mkdir(parents=True, exist_ok=True)
    free = disk_free_gb(chromium_src)
    log(f"\nUnpacking {archive.name} into {chromium_src} "
        f"(free disk: {free:.1f} GiB)...")
    _extract_with_bsdtar(archive, chromium_src, deadline)
    if not all((chromium_src / s).exists() for s in sentinels):
        log("bsdtar did not produce the full tree; trying the 7z "
            "streaming fallback.")
        staging = download_dir / f"extract-{spec['name']}"
        _extract_with_7z(
            archive,
            chromium_src,
            f"chromium-{tag}",
            staging,
            deadline,
        )
    missing = [s for s in sentinels if not (chromium_src / s).exists()]
    if missing:
        raise StageError(
            f"unpacking {archive.name} did not produce: "
            + ", ".join(missing)
        )
    free = disk_free_gb(chromium_src)
    log(f"Unpacked {archive.name} (free disk: {free:.1f} GiB).")


def ensure_git_checkout(git, chromium_src, env):
    """Give the tarball tree the .git Thorium's tooling insists on.

    The tarball tree has no .git, but Thorium setup.py's
    require_checkout() refuses a Chromium tree without one, and its
    patch series runs `git apply` inside the tree (which needs
    `git rev-parse --show-toplevel` to succeed). A bare `git init`
    satisfies both; `git apply` needs no commit.
    """
    if (chromium_src / ".git").exists():
        log("Placeholder .git already present in the Chromium tree.")
        return
    log("\nRunning git init in the tarball tree (Thorium's setup.py "
        "and patch series require a .git; the tarballs ship none).")
    run_checked([git, "init", "--quiet"], chromium_src, env=env)


def acquire_tarball_tree(args, ctx, deadline):
    """Download, verify, and unpack both Chromium tarballs."""
    build_root = ctx["build_root"]
    chromium_src = ctx["chromium_src"]
    tag = args.chromium_tag
    download_dir = build_root / TARBALL_DIR_NAME
    download_dir.mkdir(parents=True, exist_ok=True)
    free = disk_free_gb(build_root)
    log(f"\nAcquiring Chromium {tag} from the imputnet tarballs "
        f"(base + win-x64; free disk: {free:.1f} GiB).")
    for spec in TARBALLS:
        if all(
            (chromium_src / s).exists() for s in spec["sentinels"]
        ):
            log(f"The {spec['name']} tree is already unpacked; "
                "skipping its download.")
            continue
        archive = download_tarball(spec, tag, download_dir, deadline)
        extract_tarball(
            archive, spec, tag, chromium_src, download_dir, deadline
        )
        # The archive is only needed until its tree is unpacked.
        # Deleting it here keeps peak disk at tree + one archive
        # instead of tree + both archives.
        archive.unlink(missing_ok=True)
        log(f"Deleted {archive.name} (tree unpacked and verified).")
    git = find_executable("git")
    ensure_git_checkout(git, chromium_src, ctx["env"])
    marker = {
        "tag": tag,
        "sha256": {spec["name"]: spec["sha256"] for spec in TARBALLS},
    }
    (build_root / TARBALL_MARKER).write_text(
        json.dumps(marker, indent=2) + "\n", encoding="utf-8"
    )
    free = disk_free_gb(build_root)
    log(f"Chromium tarball tree ready (free disk: {free:.1f} GiB).")


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

    A fresh git clone of depot_tools is NOT initialized: its .bat
    wrappers fail with 'python3_bin_reldir.txt not found' until
    gclient or update_depot_tools has run once (this killed run #2).
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
    # Added for run #6 (run #5 died on disk: 31 GiB free vs ~45-55 GiB
    # needed). The Chromium build uses MSVC + its own CIPD clang, never
    # .NET, the preinstalled LLVM, or the VS installer package cache.
    r"C:\Program Files\dotnet",
    r"C:\Program Files\LLVM",
    r"C:\ProgramData\Microsoft\VisualStudio\Packages",
    # Added for run #7 (run #6 still filled the disk and the runner
    # crashed before keeping any log). Preinstalled servers and Perl
    # the Chromium build never uses.
    r"C:\Program Files\MongoDB",
    r"C:\Program Files\MySQL",
    r"C:\Program Files\Microsoft SQL Server",
    r"C:\Strawberry",
)
DELETABLE_JDK_ROOT = r"C:\hostedtoolcache\windows\Java_Temurin-Hotspot_jdk"

# Hostedtoolcache runtimes the build never uses, by tool name under
# C:\hostedtoolcache\windows. Deliberately NOT Python (setup-python's
# interpreter runs this script), NOT node, and NOT any Java: those are
# either in use or handled above.
HOSTEDTOOLCACHE_ROOT = r"C:\hostedtoolcache\windows"
DELETABLE_HOSTEDTOOLCACHE_NAMES = ("go", "ruby", "codeql", "pypy")

# Old Windows SDK versions under C:\Program Files (x86)\Windows
# Kits\10. The build uses exactly one SDK version — the one
# build-stage.yml installs (10.0.28000.0) — so every OTHER version
# directory under Include/Lib/bin/References is deletable. The kept
# version and the Kits root itself are never touched.
KEEP_WINDOWS_SDK_VERSION = "10.0.28000.0"
WINDOWS_KITS_SUBDIRS = ("Include", "Lib", "bin", "References")
WINDOWS_KITS_ROOT = r"C:\Program Files (x86)\Windows Kits\10"


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
    cache_root = PureWindowsPath(HOSTEDTOOLCACHE_ROOT)
    if p.is_relative_to(cache_root):
        rel = p.relative_to(cache_root)
        if rel.parts and rel.parts[0].lower() in DELETABLE_HOSTEDTOOLCACHE_NAMES:
            return True
    kits_root = PureWindowsPath(WINDOWS_KITS_ROOT)
    if p.is_relative_to(kits_root):
        rel = p.relative_to(kits_root)
        if (
            len(rel.parts) >= 2
            and rel.parts[0] in WINDOWS_KITS_SUBDIRS
            and rel.parts[1].lower() != KEEP_WINDOWS_SDK_VERSION.lower()
        ):
            return True
    return False


def free_disk_space(build_root):
    """Remove large preinstalled toolchains the Chromium build does not need.

    GitHub-hosted Windows runners start with only ~28 GiB free, but a
    Chromium checkout + build needs far more. Only the allowlisted
    toolchain dirs (Android SDK, Temurin JDKs, Haskell, Java, .NET,
    preinstalled LLVM, the VS installer package cache, and unused
    hostedtoolcache runtimes) are ever removed; _safe_to_delete refuses
    everything else. Nothing the build uses (VS toolchain, Windows SDK,
    Python, Node, git, 7z) is touched.
    """
    if os.name != "nt":
        return
    before = disk_free_gb(build_root)
    candidates = [Path(p) for p in DELETABLE_TOOLCHAINS]
    jdk_root = Path(DELETABLE_JDK_ROOT)
    if jdk_root.is_dir():
        candidates.extend(p for p in jdk_root.iterdir() if p.is_dir())
    cache_root = Path(HOSTEDTOOLCACHE_ROOT)
    if cache_root.is_dir():
        candidates.extend(
            p
            for p in cache_root.iterdir()
            if p.is_dir()
            and p.name.lower() in DELETABLE_HOSTEDTOOLCACHE_NAMES
        )
    kits_root = Path(WINDOWS_KITS_ROOT)
    for sub in WINDOWS_KITS_SUBDIRS:
        sub_dir = kits_root / sub
        if sub_dir.is_dir():
            candidates.extend(
                p
                for p in sub_dir.iterdir()
                if p.is_dir()
                and p.name.lower() != KEEP_WINDOWS_SDK_VERSION.lower()
            )
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
        item_before = disk_free_gb(build_root)
        log(f"Removing unused preinstalled toolchain: {path}")
        shutil.rmtree(path, ignore_errors=True)
        item_after = disk_free_gb(build_root)
        if item_before >= 0 and item_after >= 0:
            log(f"  Freed ~{item_after - item_before:.1f} GiB: {path}")
    docker = shutil.which("docker")
    if docker:
        item_before = disk_free_gb(build_root)
        log("Pruning Docker images/containers (docker system prune -af)...")
        try:
            subprocess.run(
                [docker, "system", "prune", "-af"],
                capture_output=True,
                timeout=600,
            )
        except (OSError, subprocess.SubprocessError) as error:
            log(f"  docker prune failed (ignored): {error}")
        item_after = disk_free_gb(build_root)
        if item_before >= 0 and item_after > item_before:
            log(f"  Freed ~{item_after - item_before:.1f} GiB: docker prune")
    after = disk_free_gb(build_root)
    if after >= 0 and before >= 0:
        log(f"Free disk: {before:.0f} GiB -> {after:.0f} GiB after cleanup.")


def ensure_depot_tools(depot_tools, env, deadline):
    """Clone depot_tools if missing, then initialize it (idempotent).

    A present, initialized checkout is left completely alone.
    depot_tools lives OUTSIDE the checkpoint zip, so this must run
    not only in the bootstrap phase but also when a stage resumes
    past bootstrap: the phase loop starts at the recorded phase and
    never re-runs bootstrap, so run #11's build-2 resumed straight
    into the build phase on a fresh runner and died with
    'autoninja.py not found in depot_tools' — ninja never started.
    """
    if not depot_tools.is_dir():
        log(f"\nCloning depot_tools into {depot_tools} "
            "(blobless partial clone; see _partial_clone)")
        if not _partial_clone(DEPOT_TOOLS_REPO, depot_tools, env=env):
            log("Partial clone of depot_tools failed; falling "
                "back to a full clone.")
            git_clone(DEPOT_TOOLS_REPO, depot_tools)
    else:
        log(f"\nUsing existing depot_tools: {depot_tools}")
    # A fresh clone is not initialized: its .bat wrappers need the
    # bundled Python (python3_bin_reldir.txt), which only appears
    # after gclient / update_depot_tools runs once.
    initialize_depot_tools(depot_tools, env, deadline)


def ensure_thorium(args, ctx, deadline):
    """Clone Thorium at the pinned commit if missing (idempotent).

    Thorium also lives outside the checkpoint zip, and the setup
    phase needs its patch series — a stage resuming at (or before)
    setup on a fresh runner must re-clone it first, the same hole
    as depot_tools in run #11. `deadline` is unused today (the
    clone is not budget-checked, exactly as in the bootstrap phase
    this was extracted from); it keeps the ensure signatures alike.
    """
    thorium_root = ctx["thorium_root"]
    env = ctx["env"]
    if thorium_root.is_dir():
        log(f"\nUsing existing Thorium checkout: {thorium_root}")
        return
    git = find_executable("git")
    log(f"\nCloning Thorium into {thorium_root} "
        "(blobless partial clone; see _partial_clone)")
    if _partial_clone(
        THORIUM_REPO, thorium_root, commit=args.thorium_commit, env=env
    ):
        run_checked(
            [git, "submodule", "update", "--init", "--recursive"],
            thorium_root,
            env=env,
        )
        return
    log("Partial clone of Thorium failed; falling back to a "
        "full clone.")
    git_clone(THORIUM_REPO, thorium_root, recursive=True)
    run_checked(
        [git, "checkout", args.thorium_commit], thorium_root, env=env
    )
    run_checked(
        [git, "submodule", "update", "--init", "--recursive"],
        thorium_root,
        env=env,
    )


def phase_bootstrap(args, ctx, deadline):
    """Clone depot_tools + Thorium, unpack the Chromium tarballs."""
    build_root = ctx["build_root"]
    chromium_src = ctx["chromium_src"]

    ensure_depot_tools(ctx["depot_tools"], ctx["env"], deadline)
    ensure_thorium(args, ctx, deadline)

    if tarball_tree_complete(build_root, chromium_src, args.chromium_tag):
        log("Chromium tarball tree already complete; skipping download.")
    else:
        acquire_tarball_tree(args, ctx, deadline)
    log("\nBootstrap phase complete.")


def phase_version(args, ctx, deadline):
    """Verify the tarball tree is the pinned Chromium version.

    The old flow checked the tag out of a shallow clone and re-ran
    gclient sync + runhooks; the tarball tree is already the pinned
    tag, post-sync and post-runhooks, so there is nothing to check
    out. Verify chrome/VERSION instead, so a wrong or corrupt tree
    fails here in build-1 rather than hours later.
    """
    chromium_src = ctx["chromium_src"]
    version_file = chromium_src / "chrome" / "VERSION"
    if not version_file.is_file():
        raise StageError(f"chrome/VERSION missing: {version_file}")
    parts = {}
    for line in version_file.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            parts[key.strip()] = value.strip()
    try:
        version = ".".join(
            parts[key] for key in ("MAJOR", "MINOR", "BUILD", "PATCH")
        )
    except KeyError as error:
        raise StageError(
            f"chrome/VERSION is missing {error}: {version_file}"
        ) from error
    if version != args.chromium_tag:
        raise StageError(
            f"tarball tree is Chromium {version}, "
            f"expected {args.chromium_tag}"
        )
    log(f"\nTarball tree verified as Chromium {version} "
        "(no checkout or gclient sync needed).")
    log("\nVersion phase complete.")


def _series_patch_targets(patch_text):
    """(modified, created) target paths of one git-style patch file."""
    modified, created = [], []
    pending_src = None
    for line in patch_text.splitlines():
        if line.startswith("--- "):
            pending_src = line[4:].split("\t", 1)[0].strip()
        elif line.startswith("+++ "):
            dst = line[4:].split("\t", 1)[0].strip()
            if dst.startswith("b/"):
                dst = dst[2:]
            if pending_src == "/dev/null":
                created.append(dst)
            elif dst != "/dev/null":
                modified.append(dst)
            pending_src = None
    return modified, created


def _analyze_series(thorium_root, chromium_src):
    """Classify every Thorium series entry against this tree.

    Returns one record per series line, in order:
    {raw, conditional, apply_root, rel, missing}. `missing` is the
    list of files the entry's patch modifies that are absent from
    the tree, Thorium's src/ overlay, and the files created by
    earlier kept entries; it is None for lines that are not
    unconditional patch entries (comments, blanks, conditional
    entries — setup.py selects none of those for the avx2 profile,
    so they are neither fetched for nor filtered).
    """
    series_path = thorium_root / "patch_scripts" / "series" / "series"
    if not series_path.is_file():
        raise StageError(f"Thorium patch series not found: {series_path}")
    overlay = set()
    overlay_root = thorium_root / "src"
    if overlay_root.is_dir():
        for path in overlay_root.rglob("*"):
            if path.is_file():
                overlay.add(path.relative_to(overlay_root).as_posix())
    created = set()
    records = []
    for raw in series_path.read_text(encoding="utf-8").splitlines():
        record = {
            "raw": raw,
            "conditional": False,
            "apply_root": ".",
            "rel": None,
            "missing": None,
        }
        line = raw.split("#", 1)[0].strip()
        if line:
            rest = line
            conditional = rest.startswith("[")
            while rest.startswith("["):
                end = rest.find("]")
                if end == -1:
                    break
                rest = rest[end + 1:].strip()
            record["conditional"] = conditional
            if not conditional and rest:
                parts = rest.split(":")
                if len(parts) == 2 and not parts[0].endswith(".patch"):
                    record["apply_root"] = parts[0].strip()
                    record["rel"] = parts[1].strip()
                else:
                    record["rel"] = rest
                patch_file = thorium_root / record["rel"]
                if patch_file.is_file():
                    modified, creates = _series_patch_targets(
                        patch_file.read_text(
                            encoding="utf-8", errors="ignore"
                        )
                    )
                    apply_root = record["apply_root"]

                    def present(path):
                        full = (
                            path
                            if apply_root == "."
                            else f"{apply_root}/{path}"
                        )
                        return (
                            (chromium_src / full).exists()
                            or full in overlay
                            or full in created
                        )

                    record["missing"] = [
                        path for path in modified if not present(path)
                    ]
                    for path in creates:
                        created.add(
                            path
                            if apply_root == "."
                            else f"{apply_root}/{path}"
                        )
        records.append(record)
    return records


def _fetch_chromium_file(tag, rel_path):
    """Download one main-repo file from the official Chromium mirrors.

    Tries chromium.googlesource.com (canonical; base64 ?format=TEXT)
    first, then the chromium/chromium GitHub mirror. Returns the
    file bytes, or None when neither mirror has the file at the tag.
    """
    quoted = urllib.parse.quote(rel_path)
    sources = (
        (
            "googlesource",
            "https://chromium.googlesource.com/chromium/src/+/refs/tags/"
            f"{tag}/{quoted}?format=TEXT",
            True,
        ),
        (
            "github",
            f"https://raw.githubusercontent.com/chromium/chromium/"
            f"{tag}/{quoted}",
            False,
        ),
    )
    for name, url, is_base64 in sources:
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "lumo-ci-stage"}
            )
            with urllib.request.urlopen(request) as response:
                body = response.read()
        except (OSError, urllib.error.URLError) as error:
            log(f"  mirror {name} failed for {rel_path}: {error}")
            continue
        if is_base64:
            try:
                body = base64.b64decode(body)
            except (ValueError, binascii.Error):
                continue
        if body:
            return body
    return None


def fetch_missing_series_files(thorium_root, chromium_src, tag):
    """Fetch patch-target files the tarballs do not ship.

    The imputnet tarballs split the tree per platform: files only
    an Android checkout carries (chrome/android/java/**,
    android_webview/**, ...) are in neither the base nor the
    win-x64 archive, but Thorium's unconditional patch series
    modifies ~60 of them, and git apply fails the whole setup when
    a target is absent. Those files belong to the main Chromium
    repo, so fetch just them, at the pinned tag, from the official
    mirrors. Files no mirror has stay missing; the series filter
    below drops their patches.
    """
    records = _analyze_series(thorium_root, chromium_src)
    wanted = []
    for record in records:
        if record["missing"] and record["apply_root"] == ".":
            for path in record["missing"]:
                if path not in wanted:
                    wanted.append(path)
    if not wanted:
        return 0
    log(f"\nFetching {len(wanted)} patch-target files the tarballs "
        f"do not ship (Chromium mirrors, tag {tag})...")
    fetched = 0
    for rel_path in wanted:
        dest = chromium_src / rel_path
        if dest.exists():
            continue
        body = _fetch_chromium_file(tag, rel_path)
        if body is None:
            log(f"  not fetchable at {tag}: {rel_path}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        fetched += 1
    log(f"Fetched {fetched}/{len(wanted)} missing patch-target files.")
    return fetched


def _test_fonts_sources(build_gn_text):
    """Extract the sources list from test_fonts' BUILD.gn text."""
    match = re.search(
        r"sources\s*=\s*\[(.*?)\]", build_gn_text, re.DOTALL
    )
    if not match:
        return []
    return re.findall(r'"([^"]+)"', match.group(1))


def _test_fonts_gcs_object(tag):
    """Parse DEPS at the tag for the test_fonts GCS pin.

    src/third_party/test_fonts/test_fonts is not a git checkout:
    DEPS declares it as a GCS dep (bucket chromium-fonts) — one
    tar.gz holding all the fonts, with the object name, size and
    sha256 pinned in DEPS itself. Returns (bucket, object_name,
    size_bytes, sha256sum), or None when not found.
    """
    body = _fetch_chromium_file(tag, "DEPS")
    if body is None:
        return None
    text = body.decode("utf-8", "replace")
    anchor = text.find("'src/third_party/test_fonts/test_fonts'")
    if anchor < 0:
        return None
    block = text[anchor:anchor + 4000]

    def grab(pattern):
        match = re.search(pattern, block)
        return match.group(1) if match else None

    bucket = grab(r"'bucket':\s*'([^']+)'")
    object_name = grab(r"'object_name':\s*'([^']+)'")
    sha256sum = grab(r"'sha256sum':\s*'([^']+)'")
    size = grab(r"'size_bytes':\s*(\d+)")
    if not all([bucket, object_name, sha256sum, size]):
        return None
    return bucket, object_name, int(size), sha256sum


def _fetch_test_fonts_archive(chromium_src, tag):
    """Download the pinned test_fonts tar.gz and extract it.

    The archive holds the font files flat at its root; gclient
    extracts it straight into third_party/test_fonts/test_fonts/,
    and so does this. Size and sha256 are verified against the
    DEPS pin before anything is written.
    """
    pin = _test_fonts_gcs_object(tag)
    if pin is None:
        raise StageError(
            "Could not parse the test_fonts GCS entry from DEPS "
            f"at tag {tag}."
        )
    bucket, object_name, size, sha256sum = pin
    url = f"https://storage.googleapis.com/{bucket}/{object_name}"
    log(f"Downloading the test_fonts archive ({size} bytes, "
        "pinned in DEPS)...")
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "lumo-ci-stage"}
        )
        with urllib.request.urlopen(request) as response:
            blob = response.read()
    except (OSError, urllib.error.URLError) as error:
        raise StageError(
            f"test_fonts archive download failed: {error}"
        ) from error
    if len(blob) != size:
        raise StageError(
            f"test_fonts archive size mismatch: got {len(blob)} "
            f"bytes, DEPS pins {size}."
        )
    if hashlib.sha256(blob).hexdigest() != sha256sum:
        raise StageError(
            "test_fonts archive sha256 does not match the DEPS pin."
        )
    dest_root = (
        chromium_src / "third_party" / "test_fonts" / "test_fonts"
    )
    dest_root.mkdir(parents=True, exist_ok=True)
    extracted = 0
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as arc:
        for member in arc.getmembers():
            name = member.name
            if name.startswith("./"):
                name = name[2:]
            if not member.isfile() or not name or "/" in name:
                continue
            source = arc.extractfile(member)
            if source is None:
                continue
            (dest_root / name).write_bytes(source.read())
            extracted += 1
    log(f"Extracted {extracted} test_fonts files "
        "(size and sha256 verified against DEPS).")
    return extracted


def fetch_missing_test_fonts(chromium_src, tag):
    """Fetch third_party/test_fonts files the tarballs do not ship.

    Run #8 (2026-11-13) got all the way through setup and gn gen,
    then autoninja stopped during scheduling:
    third_party/test_fonts/test_fonts/Ahem.ttf was absent
    ("missing and no known rule to make it"). The imputnet base
    tarball contains no third_party/test_fonts entries at all,
    but thorium_all pulls in content_shell -> web_test -> skia's
    test_fonts_harddeps, so every file in the copy("test_fonts")
    target is a build input. The fonts are not in the main
    Chromium repo either: DEPS ships them as a separate GCS
    tar.gz (see _test_fonts_gcs_object), which the tarball build
    never fetched. Read the target's source list from
    third_party/test_fonts/BUILD.gn at the pinned tag, fetch the
    main-repo files (LICENSE) from the mirrors and the fonts
    from the pinned GCS archive, and fail here in the setup
    phase rather than inside autoninja.
    """
    fonts_root = chromium_src / "third_party" / "test_fonts"
    build_gn_path = fonts_root / "BUILD.gn"
    if build_gn_path.is_file():
        build_gn_text = build_gn_path.read_text(
            encoding="utf-8", errors="replace"
        )
    else:
        body = _fetch_chromium_file(
            tag, "third_party/test_fonts/BUILD.gn"
        )
        if body is None:
            raise StageError(
                "third_party/test_fonts/BUILD.gn is absent from "
                "the tarball tree and could not be fetched from "
                f"the Chromium mirrors at tag {tag}."
            )
        build_gn_path.parent.mkdir(parents=True, exist_ok=True)
        build_gn_path.write_bytes(body)
        build_gn_text = body.decode("utf-8", "replace")
    wanted = _test_fonts_sources(build_gn_text)
    if not wanted:
        raise StageError(
            "Parsed no sources from third_party/test_fonts/"
            "BUILD.gn; refusing to continue with an incomplete "
            "test_fonts tree."
        )
    missing = [
        rel for rel in wanted if not (fonts_root / rel).is_file()
    ]
    if not missing:
        log(f"\ntest_fonts: all {len(wanted)} files already present.")
        return 0
    log(f"\nFetching {len(missing)} test_fonts files the tarballs "
        f"do not ship (tag {tag})...")
    fetched = 0
    needs_archive = False
    for rel_path in missing:
        if rel_path.startswith("test_fonts/"):
            needs_archive = True
            continue
        body = _fetch_chromium_file(
            tag, f"third_party/test_fonts/{rel_path}"
        )
        if body is None:
            raise StageError(
                f"test_fonts file no mirror has at {tag}: {rel_path}"
            )
        dest = fonts_root / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        fetched += 1
    if needs_archive:
        fetched += _fetch_test_fonts_archive(chromium_src, tag)
    still_missing = [
        rel for rel in wanted if not (fonts_root / rel).is_file()
    ]
    if still_missing:
        shown = ", ".join(still_missing[:5])
        if len(still_missing) > 5:
            shown += f", ... ({len(still_missing)} files)"
        raise StageError(
            f"test_fonts still incomplete after fetching: {shown}"
        )
    log(f"test_fonts complete: {len(wanted)} files present "
        f"({fetched} fetched).")
    return fetched


def filter_unappliable_series(thorium_root, chromium_src):
    """Drop Thorium series entries that still cannot apply.

    Runs after fetch_missing_series_files. git apply is atomic per
    patch file, so one dead target would fail Thorium's whole
    setup phase; this filter is the safety net for entries whose
    targets are still absent (a file no mirror has and no earlier
    patch creates). In local validation against the pinned Thorium
    commit (2026-11-13), the fetch step completed the tree and
    this filter dropped nothing — setup.py then ran end to end.
    Any drop is logged loudly, here and in the stage log.
    """
    series_path = thorium_root / "patch_scripts" / "series" / "series"
    records = _analyze_series(thorium_root, chromium_src)
    kept_lines = []
    dropped = []
    for record in records:
        if record["missing"]:
            dropped.append((record["rel"], record["missing"]))
            continue
        kept_lines.append(record["raw"])
    if dropped:
        for rel, missing in dropped:
            shown = ", ".join(missing[:3])
            if len(missing) > 3:
                shown += f", ... ({len(missing)} files)"
            log(f"WARNING: dropping Thorium series entry {rel}: it "
                f"modifies files absent from this tree ({shown}). "
                "Keeping it would fail the entire setup phase.")
        # write_bytes, never write_text: text mode on Windows would
        # rewrite this build input with CRLF line endings (the
        # run #13/#14 bug class). Thorium's apply_series.py reads
        # with splitlines() and would tolerate CRLF today, but the
        # file's bytes are pinned to LF so no future byte-exact
        # consumer can be surprised by them.
        series_path.write_bytes(
            ("\n".join(kept_lines) + "\n").encode("utf-8")
        )
        log(f"Patch series filtered: dropped {len(dropped)} "
            "unappliable entries (see warnings above).")
    return dropped


# ---------------------------------------------------------------------------
# Lumo series overrides and branding (run #13)
#
# Two adjustments happen around Thorium's setup.py:
#
#  * BEFORE setup.py: apply_lumo_series_overrides() edits the fresh
#    Thorium clone's patch series. The 2026-11-15 audit of Thorium's
#    152 patches found four whose behavior Lumo does not ship
#    (download quarantine removal, FTP on by default, DoH bypassing
#    managed/parental DNS controls, and the first-run auto-open of
#    thorium.rocks), plus one patch (#121) whose UNCONDITIONAL part
#    (silencing Safe Browsing dangerous-download reports) must be
#    undone while its switch-gated part may stay. Dropping a series
#    entry means Thorium's setup never applies that patch at all.
#
#  * AFTER setup.py: apply_lumo_branding() turns the Thorium tree
#    into Lumo: Lumo's own git patches (scripts/ci/lumo_patches/ —
#    install_static identity incl. the Lumo_Browser user-data
#    directory, the BRANDING file, the version page credits, the
#    Thorium 2024 UI default-on flip), the decided flat "L" icon
#    copied over Thorium's theme resources (plus the FRE logos
#    and the version-page wordmarks), and targeted GRD/XTB string
#    replacements (product name, About company/copyright,
#    first-profile name, the Chrome Labs title, the flags page
#    title, the FRE sign-in title — the last two with their ja
#    translations re-keyed to the new grit ids, since a body edit
#    moves the fingerprint the runtime looks translations up by),
#    then the systematic Thorium -> Lumo pass over the desktop
#    GRD/GRDP files (lumo_systematic_brand_pass: the 2026-11-19
#    audit found ~945 branded bodies the targeted edits cannot
#    cover), the same replacement for the chrome://flags entry
#    header's display strings, and the Labs origin suffixes
#    (" (Thorium)" / " (Lumo)") on the Labs experiment names.
#    GRD strings are edited by message name rather than by patch
#    because Thorium's GRD rebase rewrites them during setup.py, so
#    their exact post-setup wording is Thorium-version data, not
#    something a static patch context should depend on.
#
# Idempotency (the setup phase may re-run after a budget expiry):
# the series overrides re-derive from the fresh Thorium clone (and
# are no-ops on an already-edited clone), the git patches use a
# both-ways check like Thorium's apply_series.py (reverse check
# passes = already applied, skip), the icon
# copies are plain overwrites, and every string replacement treats
# "already replaced" as success.
# ---------------------------------------------------------------------------

LUMO_SERIES_DROPS = (
    # Thorium patch audit (2026-11-15) corrections; the numbers are
    # the audit's patch indices.
    "other/disable-download-quarantine.patch",        # #122
    "other/ftp-support-thorium.patch",                # #67
    "other/keep-doh-enabled-on-managed-devices.patch",  # #112
    "other/thorium-first-run-welcome.patch",          # #71
)

LUMO_SB_REPORT_PATCH = "other/allow-insecure-downloads.patch"  # #121
LUMO_SB_REPORT_TARGET = (
    "chrome/browser/download/chrome_download_manager_delegate.cc"
)


def drop_lumo_series_entries(thorium_root):
    """Drop the LUMO_SERIES_DROPS entries from Thorium's series."""
    series_path = thorium_root / "patch_scripts" / "series" / "series"
    if not series_path.is_file():
        raise StageError(f"Thorium patch series not found: {series_path}")
    kept_lines = []
    dropped = []
    for raw in series_path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        rel = None
        if line and not line.startswith("["):
            parts = line.split(":")
            if len(parts) == 2 and not parts[0].endswith(".patch"):
                rel = parts[1].strip()
            else:
                rel = line
        if rel in LUMO_SERIES_DROPS:
            dropped.append(rel)
            continue
        kept_lines.append(raw)
    if dropped:
        # Write bytes, not text: on the Windows CI runner, text-mode
        # writes translate "\n" to "\r\n", and a CRLF series file
        # would still parse, but keeping the file LF-only avoids any
        # strict downstream consumer tripping over carriage returns.
        series_path.write_bytes(
            ("\n".join(kept_lines) + "\n").encode("utf-8")
        )
        for rel in dropped:
            log(f"Lumo series override: dropping {rel} "
                "(audit correction; see stage.py header).")
    return dropped


def restore_safe_browsing_reporting(thorium_root):
    """Undo the unconditional part of Thorium patch #121.

    allow-insecure-downloads.patch gates most of its behavior
    behind the --allow-insecure-downloads switch (acceptable), but
    one hunk stubs MaybeSendDangerousDownloadOpenedReport /
    MaybeSendDangerousDownloadCanceledReport to `return;`
    UNCONDITIONALLY, so Safe Browsing never hears about dangerous
    downloads even without the switch. Remove exactly that hunk
    from the clone's copy of the patch; the rest of the patch is
    untouched. Returns True when the hunk was removed.
    """
    patch_path = thorium_root / LUMO_SB_REPORT_PATCH
    if not patch_path.is_file():
        raise StageError(f"Thorium patch not found: {patch_path}")
    text = patch_path.read_text(encoding="utf-8")
    if "MaybeSendDangerousDownloadOpenedReport" not in text:
        log("Safe Browsing reporting already restored in "
            f"{LUMO_SB_REPORT_PATCH}; skipping.")
        return False
    sections = re.split(r"(?m)^(?=diff --git )", text)
    out = []
    removed = 0
    for section in sections:
        if not section.startswith("diff --git "):
            out.append(section)
            continue
        header = section.split("\n", 1)[0]
        if f" b/{LUMO_SB_REPORT_TARGET}" not in header:
            out.append(section)
            continue
        chunks = re.split(r"(?m)^(?=@@ )", section)
        kept_chunks = []
        for chunk in chunks:
            if (chunk.startswith("@@")
                    and "MaybeSendDangerousDownload" in chunk):
                removed += 1
                continue
            kept_chunks.append(chunk)
        out.append("".join(kept_chunks))
    if removed != 1:
        raise StageError(
            f"expected to remove exactly 1 Safe Browsing report hunk "
            f"from {LUMO_SB_REPORT_PATCH}, removed {removed}; the "
            "pinned Thorium patch format may have changed.")
    new_text = "".join(out)
    if ("MaybeSendDangerousDownloadOpenedReport" in new_text
            or "ShouldBlockFile" not in new_text):
        raise StageError(
            "Safe Browsing report restoration produced an "
            "unexpected patch; refusing to continue.")
    # Write bytes, not text: on the Windows CI runner, text-mode
    # writes translate "\n" to "\r\n", and git apply cannot apply a
    # CRLF patch (this is what failed build #13: the edited patch
    # failed `git apply --check` although its content was correct).
    # write_bytes is newline-translation-free on every platform.
    patch_path.write_bytes(new_text.encode("utf-8"))
    log("Lumo series override: restored Safe Browsing "
        "dangerous-download reporting (removed the unconditional "
        f"stub hunk from {LUMO_SB_REPORT_PATCH}; the "
        "--allow-insecure-downloads switch behavior is unchanged).")
    return True


def apply_lumo_series_overrides(thorium_root):
    """All pre-setup edits to the Thorium clone (audit corrections)."""
    drop_lumo_series_entries(thorium_root)
    restore_safe_browsing_reporting(thorium_root)


LUMO_ICON_COPIES = (
    # (asset in scripts/ci/lumo_branding/, destination in the tree)
    ("app.ico", "chrome/app/theme/chromium/win/chromium.ico"),
    ("app.ico", "chrome/app/theme/chromium/win/chromium_doc.ico"),
    ("app.ico", "chrome/app/theme/chromium/win/chromium_pdf.ico"),
    ("product_logo_16.png",
     "chrome/app/theme/chromium/product_logo_16.png"),
    ("product_logo_24.png",
     "chrome/app/theme/chromium/product_logo_24.png"),
    ("product_logo_48.png",
     "chrome/app/theme/chromium/product_logo_48.png"),
    ("product_logo_64.png",
     "chrome/app/theme/chromium/product_logo_64.png"),
    ("product_logo_128.png",
     "chrome/app/theme/chromium/product_logo_128.png"),
    ("product_logo_256.png",
     "chrome/app/theme/chromium/product_logo_256.png"),
    ("product_logo_16.png",
     "chrome/app/theme/default_100_percent/chromium/product_logo_16.png"),
    ("product_logo_32.png",
     "chrome/app/theme/default_100_percent/chromium/product_logo_32.png"),
    # The 200% directory stores physical pixels: its "16" file is
    # 32px and its "32" file is 64px (verified against Thorium's
    # overlay), so the assets shift one size up.
    ("product_logo_32.png",
     "chrome/app/theme/default_200_percent/chromium/product_logo_16.png"),
    ("product_logo_64.png",
     "chrome/app/theme/default_200_percent/chromium/product_logo_32.png"),
    ("tiles_logo.png", "chrome/app/theme/chromium/win/tiles/Logo.png"),
    ("tiles_small_logo.png",
     "chrome/app/theme/chromium/win/tiles/SmallLogo.png"),
    # First-run (FRE) logos: Thorium ships its circular logo as
    # product_logo.svg, shown on the sign-in page; #13 left the
    # SVG form untouched and the #18 build's FRE still showed it.
    # The animation variant (FRE refresh) gets a static Lumo mark
    # too — a static logo in place of the animation is a
    # deliberate simplification, not an oversight — but NOT the
    # same file: the FRE stylesheet renders the animation svg at
    # twice the logo size and crops the central half, because
    # upstream's file is 50% padding by design ("Because 50% of
    # the svg file is the padding, we scale it by 2" in
    # sign_in_promo_refresh.css). The plain Lumo mark has no
    # padding, so it overflowed the logo box on all sides and
    # overlapped the welcome heading in build #29's real-machine
    # check. product_logo_animation.svg therefore carries the
    # same mark centred in a doubled viewBox (the artwork
    # occupies exactly the central 50%).
    ("product_logo.svg", "chrome/app/theme/chromium/product_logo.svg"),
    ("product_logo_animation.svg",
     "chrome/app/theme/chromium/product_logo_animation.svg"),
    # Version-page wordmarks (icon + "Lumo" text, 342x64; the
    # white variant is shown on dark backgrounds). Thorium's
    # overlay plants these under components/resources and #13
    # only covered chrome/app/theme, so the #18 version page
    # still showed the "thorium" wordmark. The 200% files are
    # the same 342x64 in Thorium's overlay (verified), so the
    # same assets serve both densities.
    ("wordmark.png",
     "components/resources/default_100_percent/chromium/product_logo.png"),
    ("wordmark_white.png",
     "components/resources/default_100_percent/chromium/"
     "product_logo_white.png"),
    ("wordmark.png",
     "components/resources/default_200_percent/chromium/product_logo.png"),
    ("wordmark_white.png",
     "components/resources/default_200_percent/chromium/"
     "product_logo_white.png"),
    # Payment-dialog wordmarks (IDR_PRODUCT_LOGO_NAME_22 /
    # _WHITE): theme_resources.grd loads these from the theme
    # dirs, not components/resources, so the copies above never
    # reached them and the payment dialog kept the "chromium"
    # wordmark. The upstream files are 97x22 at 100% and 194x44
    # at 200%; the assets keep those canvas sizes (the 200%
    # assets are named for their 44px height, like the
    # product_logo_<size> assets above).
    ("product_logo_name_22.png",
     "chrome/app/theme/default_100_percent/chromium/"
     "product_logo_name_22.png"),
    ("product_logo_name_22_white.png",
     "chrome/app/theme/default_100_percent/chromium/"
     "product_logo_name_22_white.png"),
    ("product_logo_name_44.png",
     "chrome/app/theme/default_200_percent/chromium/"
     "product_logo_name_22.png"),
    ("product_logo_name_44_white.png",
     "chrome/app/theme/default_200_percent/chromium/"
     "product_logo_name_22_white.png"),
)

# Preinstalled extension packages (patch 0027): signed CRX files
# committed under scripts/ci/lumo_extensions/ (see the README
# there), copied into the tree so the component resources GRD can
# embed them. At runtime the browser writes each CRX into new
# profiles and registers it as an external extension
# (chrome/browser/extensions/preinstalled_extensions.cc), the
# same seeding design as the bundled uBlock Origin. The
# destination directory is Lumo-only, so the copy creates it.
LUMO_EXTENSION_CRX_COPIES = (
    # (asset in scripts/ci/lumo_extensions/, destination in the tree)
    ("chrome_info_viewer_1.0.0.crx",
     "chrome/browser/resources/chrome_info_viewer/"
     "chrome_info_viewer_1.0.0.crx"),
)

# grit translation fingerprint of the message "Thorium Experiments"
# (IDS_WINDOW_TITLE_EXPERIMENTS, the Chrome Labs bubble title), as
# a signed-md5 grit id: int(md5(...).hexdigest()[:16], 16) with the
# sign masked off. The fingerprint is of the POST-rebase English
# body: Thorium's GRD rebase (sync_grd_strings.py, run by setup.py
# before this branding step) rewrites the GRD body "Experiments"
# to "Thorium Experiments", which changes the message's grit id,
# and copies the ja translation to the NEW id with the body
# "Thorium 試験運用機能" (it prepends "Thorium" for this message).
# The pre-rebase id 5199729219167945352 (fingerprint of
# "Experiments") stays behind in the XTB as an orphaned entry with
# the body "試験運用機能" and must NOT be targeted. Lumo's decided
# form keeps Thorium visible in parentheses.
LUMO_LABS_TITLE_XTB_ID = "3668957331916824695"
LUMO_LABS_TITLE_JA = "Lumo 試験運用機能（Thorium 試験運用機能）"

# Lumo's GRD edit renames the Labs title body itself to
# "Lumo Experiments (Thorium Experiments)", which moves its grit
# fingerprint AGAIN: the runtime looks the ja translation up
# under the NEW id, so the XTB entry at LUMO_LABS_TITLE_XTB_ID is
# orphaned by Lumo's own edit and the title falls back to English
# (seen on the #18 build: the Labs bubble title rendered in
# English). The XTB entry must be re-keyed to the new id; see
# _retarget_xtb_translation. Both ids below are grit fingerprints
# in the tclib.GenerateMessageId form (no meaning for this
# message), recomputed and cross-checked against the real files.
LUMO_LABS_TITLE_EN = "Lumo Experiments (Thorium Experiments)"
LUMO_LABS_TITLE_XTB_ID_NEW = "8435388291773966912"

# The first-run (FRE) sign-in heading, IDS_FRE_SIGN_IN_TITLE_0 in
# chromium_strings.grd: Thorium's rebase rewrote the body to
# "Sign in to Thorium" directly (not a product-name placeholder),
# so the product-name replacements never touched it and the #18
# build's FRE still said "Thorium にログイン". The message carries
# meaning="Title", which grit folds into the fingerprint
# (tclib.GenerateMessageId combines the message and meaning
# fingerprints), so the ids differ from a plain-body fingerprint.
# Old id: GenerateMessageId("Sign in to Thorium", "Title"); new
# id: GenerateMessageId("Sign in to Lumo", "Title"). Both were
# recomputed with grit's own formula and the old one matches the
# entry actually present in chromium_strings_ja.xtb.
LUMO_FRE_TITLE_XTB_ID = "9205156301846564163"
LUMO_FRE_TITLE_XTB_ID_NEW = "4827836723507820919"
LUMO_FRE_TITLE_JA = "Lumo にログイン"


def _replace_grd_message(text, name, expected_bodies, new_body, desc):
    """Replace the body of every <message name=...> whose stripped
    body is in expected_bodies. Returns (new_text, count). Raises
    StageError when no matching message exists and the new body is
    not already present (i.e. the string drifted unexpectedly)."""
    pattern = re.compile(
        r'(<message name="' + re.escape(name) + r'"[^>]*>)(.*?)(</message>)',
        re.S,
    )
    count = 0
    already = 0

    def repl(match):
        nonlocal count, already
        body = match.group(2)
        stripped = body.strip()
        if stripped == new_body:
            already += 1
            return match.group(0)
        if stripped in expected_bodies:
            count += 1
            return (match.group(1)
                    + body.replace(stripped, new_body, 1)
                    + match.group(3))
        return match.group(0)

    new_text = pattern.sub(repl, text)
    if count == 0 and already == 0:
        raise StageError(
            f"{desc}: no <message name=\"{name}\"> with an expected "
            f"body {sorted(expected_bodies)} found; the string may "
            "have drifted in the pinned Thorium/Chromium.")
    return new_text, count


def _replace_copyright_message(text, desc):
    """IDS_ABOUT_VERSION_COPYRIGHT: keep the <ph name="YEAR">
    element, replace the author tail after it."""
    pattern = re.compile(
        r'(<message name="IDS_ABOUT_VERSION_COPYRIGHT"[^>]*>)'
        r"(.*?)"
        r"(</message>)",
        re.S,
    )
    new_tail = (" seasea1010. Based on Thorium and Chromium "
                "(BSD License).")
    count = 0
    already = 0

    def repl(match):
        nonlocal count, already
        body = match.group(2)
        if new_tail in body:
            already += 1
            return match.group(0)
        if "The Chromium Authors" not in body \
                and "The Thorium Authors" not in body:
            return match.group(0)  # e.g. the Google-branded variant
        new_body, n = re.subn(
            r"(</ph>)[^<]*", lambda m: m.group(1) + new_tail, body)
        if n != 1:
            return match.group(0)
        count += 1
        return match.group(1) + new_body + match.group(3)

    new_text = pattern.sub(repl, text)
    if count == 0 and already == 0:
        raise StageError(
            f"{desc}: IDS_ABOUT_VERSION_COPYRIGHT with a Chromium/"
            "Thorium author tail not found; the string may have "
            "drifted in the pinned Thorium/Chromium.")
    return new_text, count


def _replace_xtb_translation(text, translation_id, new_body, desc):
    """Replace one <translation id=...> body in an XTB file."""
    pattern = re.compile(
        r'(<translation id="' + re.escape(translation_id) + r'">)'
        r"(.*?)"
        r"(</translation>)",
        re.S,
    )
    match = pattern.search(text)
    if not match:
        raise StageError(
            f"{desc}: translation id {translation_id} not found; "
            "the string may have drifted in the pinned Thorium.")
    if match.group(2) == new_body:
        return text, 0
    return (text[:match.start(2)] + new_body + text[match.end(2):]), 1


def _retarget_xtb_translation(text, old_id, new_id, new_body, desc):
    """Re-key one <translation> entry to a new grit id, setting
    its body. Returns (new_text, changes).

    Needed whenever Lumo edits an English GRD body whose ja
    translation must follow: grit derives the translation id from
    the English body (plus the meaning, when the message has one),
    so after the body edit the runtime looks the translation up
    under the NEW id. An entry left at the old id is orphaned and
    the string falls back to English — this is exactly what
    happened to the Chrome Labs title on the #18 build.

    Idempotent: an entry already at new_id with new_body is
    success (a stale entry still sitting at old_id is dropped);
    an entry at new_id with a different body has its body
    replaced; otherwise the entry at old_id is re-keyed. Raises
    StageError when neither id is present.
    """
    entry_re = r'<translation id="%s">(.*?)</translation>'

    def find(tid):
        return re.search(entry_re % re.escape(tid), text, re.S)

    new_match = find(new_id)
    old_match = find(old_id) if old_id != new_id else None
    if new_match is not None:
        changes = 0
        if old_match is not None:
            text = (text[:old_match.start()] + text[old_match.end():])
            changes += 1
            new_match = find(new_id)
        if new_match.group(1) != new_body:
            text = (text[:new_match.start(1)] + new_body
                    + text[new_match.end(1):])
            changes += 1
        return text, changes
    if old_match is None:
        raise StageError(
            f"{desc}: neither translation id {old_id} nor {new_id} "
            "found; the string may have drifted in the pinned "
            "Thorium.")
    text = (text[:old_match.start()]
            + f'<translation id="{new_id}">{new_body}</translation>'
            + text[old_match.end():])
    return text, 1


def lumo_string_replacements(chromium_src):
    """Targeted GRD/XTB string edits for the Lumo brand (post-setup).

    Returns a list of (description, replacements) for logging.
    Raises StageError on unexpected drift — a loud failure here is
    better than silently shipping Thorium strings.
    """
    results = []

    path = chromium_src / "chrome/app/chromium_strings.grd"
    text = path.read_text(encoding="utf-8")
    total = 0
    for name, expected, new in (
        ("IDS_PRODUCT_NAME", ("Chromium", "Thorium"), "Lumo"),
        ("IDS_SHORT_PRODUCT_NAME", ("Chromium", "Thorium"), "Lumo"),
        ("IDS_ABOUT_VERSION_COMPANY_NAME",
         ("The Chromium Authors", "The Thorium Authors"), "seasea1010"),
    ):
        text, n = _replace_grd_message(
            text, name, set(expected), new, str(path))
        total += n
    text, n = _replace_copyright_message(text, str(path))
    total += n
    if total:
        path.write_text(text, encoding="utf-8")
    results.append(("chromium_strings.grd (product name, About "
                     "company/copyright)", total))

    # Same file: the first-run sign-in heading names the product
    # directly in its body (see LUMO_FRE_TITLE_XTB_ID), so it
    # needs its own replacement; counted separately from the
    # product-name block above.
    text = path.read_text(encoding="utf-8")
    text, n = _replace_grd_message(
        text, "IDS_FRE_SIGN_IN_TITLE_0",
        {"Sign in to Thorium", "Sign in to Chromium"},
        "Sign in to Lumo", str(path))
    if n:
        path.write_text(text, encoding="utf-8")
    results.append(("chromium_strings.grd (FRE sign-in title)", n))

    path = chromium_src / "chrome/app/generated_resources.grd"
    text = path.read_text(encoding="utf-8")
    # Thorium's GRD rebase has already rewritten the body to
    # "Thorium Experiments" by the time this runs (build #15
    # failed here when only the pristine body was accepted);
    # accept the pristine "Experiments" too, in case the rebase
    # ever stops renaming it.
    text, n = _replace_grd_message(
        text, "IDS_WINDOW_TITLE_EXPERIMENTS",
        {"Experiments", "Thorium Experiments"},
        LUMO_LABS_TITLE_EN, str(path))
    if n:
        path.write_text(text, encoding="utf-8")
    results.append(("generated_resources.grd (Chrome Labs title)", n))

    # The chrome://flags page heading is a DIFFERENT string from
    # the Labs title: IDS_FLAGS_UI_TITLE in
    # components/flags_strings.grdp (the #18 build still showed
    # "Thorium Experiments" there). It is translateable="false",
    # so the English body is what every locale renders and no
    # XTB entry exists to re-key. Same decided form as the Labs
    # title, for one consistent experiments brand.
    path = chromium_src / "components/flags_strings.grdp"
    text = path.read_text(encoding="utf-8")
    text, n = _replace_grd_message(
        text, "IDS_FLAGS_UI_TITLE",
        {"Experiments", "Thorium Experiments"},
        LUMO_LABS_TITLE_EN, str(path))
    if n:
        path.write_text(text, encoding="utf-8")
    results.append(("flags_strings.grdp (flags page title)", n))

    path = chromium_src / "chrome/app/profiles_strings.grdp"
    text = path.read_text(encoding="utf-8")
    text, n = _replace_grd_message(
        text, "IDS_DEFAULT_PROFILE_NAME", {"First user"},
        "Lumo-Test-Account", str(path))
    if n:
        path.write_text(text, encoding="utf-8")
    results.append(("profiles_strings.grdp (first profile name)", n))

    path = (chromium_src
            / "chrome/app/resources/generated_resources_ja.xtb")
    text = path.read_text(encoding="utf-8")
    # Re-key, not just re-body: Lumo's GRD edit moved the Labs
    # title's grit id to LUMO_LABS_TITLE_XTB_ID_NEW, so replacing
    # the body at the old id (the #13 approach) left the ja
    # translation orphaned and the title fell back to English.
    text, n = _retarget_xtb_translation(
        text, LUMO_LABS_TITLE_XTB_ID, LUMO_LABS_TITLE_XTB_ID_NEW,
        LUMO_LABS_TITLE_JA, str(path))
    if n:
        path.write_text(text, encoding="utf-8")
    results.append(("generated_resources_ja.xtb (Labs title, ja)", n))

    path = (chromium_src
            / "chrome/app/resources/chromium_strings_ja.xtb")
    text = path.read_text(encoding="utf-8")
    text, n = _retarget_xtb_translation(
        text, LUMO_FRE_TITLE_XTB_ID, LUMO_FRE_TITLE_XTB_ID_NEW,
        LUMO_FRE_TITLE_JA, str(path))
    if n:
        path.write_text(text, encoding="utf-8")
    results.append(("chromium_strings_ja.xtb (FRE sign-in title, "
                    "ja)", n))

    return results


# ---------------------------------------------------------------------------
# Systematic Thorium -> Lumo brand pass (build #24, task 19).
#
# The 2026-11-19 full audit of the branded tree
# (goals/cef-chromium/files/lumo-thorium-strings-audit.md) found
# ~945 desktop GRD/GRDP messages whose bodies still name Thorium:
# Thorium's GRD rebase writes the product name literally into
# message bodies, so the targeted replacements above can never
# keep up. This pass replaces the word "Thorium" with "Lumo" in
# the TEXT of every desktop message that actually ships in the
# Windows build, and re-keys the Japanese translations to the
# new grit ids (a body edit moves the fingerprint the runtime
# looks translations up by; see _retarget_xtb_translation).
#
# Scope and protections (audit classes B and C):
#  * Only the files in LUMO_SYSTEMATIC_GRD_FILES are touched
#    (desktop chrome/app + components string files). Android and
#    ChromeOS string files are not in the list at all.
#  * A message wrapped in <if> gates is edited only when every
#    gate evaluates true for the Windows build (evaluated against
#    LUMO_GRD_WINDOWS_DEFINES), so variants for other platforms,
#    the Google-branded flavor, Chrome for Testing and titlecase
#    variants are left alone.
#  * Excluded by name or body (class B — keeping "Thorium" is
#    CORRECT there): the decided BSD attribution ("Based on
#    Thorium and Chromium"), the decided bilingual Labs/flags
#    titles, the Thorium 2024 UI experiment NAME, Lumo's own
#    strings that reference the real Thorium browser/profile
#    (IDS_LUMO_* / *_LUMO_*), the updater service names (no
#    updater ships), "ThoriumOS", the "Not used in Thorium"
#    placeholders, and bodies already carrying a Labs origin
#    suffix from lumo_labs_origin_suffix_pass below.
#  * desc/meaning attributes, comments and <ph> element content
#    are never touched — only message text nodes.
#
# The word match is ASCII-case-sensitive "Thorium" not flanked
# by ASCII letters or digits, applied to the raw text segments OUTSIDE
# <ph> elements: in the raw body a placeholder tag boundary is
# a word boundary (the user sees "sign in to Thorium" as words
# even where a link placeholder follows), while grit's
# presentable content would glue the placeholder name onto the
# word. New grit ids are therefore always computed from the
# EDITED segment re-parsed, never from a string substitution on
# the presentable content.
#
# Idempotent: after one run the edited bodies contain no
# "Thorium" word in their text nodes, the exclusions cover the
# intentional remainder, and the JA entries already sit at the
# new ids (a re-run finds no candidates and no stale entries).
# The loud check is a post-pass invariant — zero unprotected
# Windows-build messages still containing the word — rather
# than an edit count, so a branding re-run (which legitimately
# edits nothing) passes while a silently broken pass on a
# fresh tree fails. Measured on the pinned tree: 819 EN
# messages edited, 721 ja XTB entries re-keyed or duplicated
# (see _retarget_xtb_entry for the shared-id duplication).
# ---------------------------------------------------------------------------

LUMO_SYSTEMATIC_GRD_FILES = (
    "chrome/app/app_management_strings.grdp",
    "chrome/app/chromium_strings.grd",
    "chrome/app/extensions_strings.grdp",
    "chrome/app/generated_resources.grd",
    "chrome/app/settings_chromium_strings.grdp",
    "chrome/app/settings_strings.grdp",
    "chrome/app/shared_settings_strings.grdp",
    "components/autofill_payments_strings.grdp",
    "components/autofill_strings.grdp",
    "components/components_chromium_strings.grd",
    "components/error_page_strings.grdp",
    "components/flags_strings.grdp",
    "components/heavy_ad_intervention_strings.grdp",
    "components/management_strings.grdp",
    "components/new_or_sad_tab_strings.grdp",
    "components/page_info_strings.grdp",
    "components/reset_password_strings.grdp",
    "components/security_interstitials_strings.grdp",
    "components/ssl_errors_strings.grdp",
    "components/version_ui_strings.grdp",
)

# Japanese XTBs the re-keyed translations can live in. An id is
# looked up in ALL of them: a message's translation occasionally
# sits in a sibling GRD's XTB (IDS_FLAGS_UI_RELAUNCH_NOTICE lives
# in components_chromium_strings.grd but its ja entry exists in
# chromium_strings_ja.xtb as well). The Google-Chrome-branded
# XTBs are a different channel and are not searched.
LUMO_SYSTEMATIC_JA_XTB_FILES = (
    "chrome/app/resources/chromium_strings_ja.xtb",
    "chrome/app/resources/generated_resources_ja.xtb",
    "components/strings/components_strings_ja.xtb",
    "components/strings/components_chromium_strings_ja.xtb",
    "components/strings/privacy_sandbox_strings_ja.xtb",
)

# grit <if expr> defines for the Windows desktop build Lumo
# ships. Unknown identifiers (a future feature define) make the
# gate pass, biasing toward replacement; every platform define
# is listed, so other-platform variants are still excluded.
LUMO_GRD_WINDOWS_DEFINES = {
    "is_win": True,
    "is_macosx": False,
    "is_linux": False,
    "is_ios": False,
    "is_android": False,
    "is_chromeos": False,
    "is_fuchsia": False,
    "is_posix": False,
    "is_cfm": False,
    "reven": False,
    "_google_chrome": False,
    "_is_chrome_for_testing_branded": False,
    "toolkit_views": True,
    "use_titlecase": False,
    "use_blink": True,
    "enable_extensions": True,
    "enable_extensions_core": True,
    "enable_pdf": True,
    "enable_printing": True,
    "enable_vr": True,
    "enable_webui_ntp": True,
    "chrome_root_store_cert_management_ui": False,
}

LUMO_SYSTEMATIC_NAME_EXCLUDES = frozenset({
    # Decided BSD attribution form (also caught by the
    # "Based on Thorium" body rule once the targeted
    # replacement above has run).
    "IDS_ABOUT_VERSION_COPYRIGHT",
    # Decided bilingual titles: "Lumo Experiments
    # (Thorium Experiments)" / "Lumo 試験運用機能（Thorium
    # 試験運用機能）".
    "IDS_WINDOW_TITLE_EXPERIMENTS",
    "IDS_FLAGS_UI_TITLE",
    # The experiment name itself is kept by decision; only its
    # description (which names no product) is unaffected anyway.
    "IDS_CHROMELABS_THORIUM_2024_NAME",
})

_THORIUM_WORD_RE = re.compile(
    r"(?<![A-Za-z0-9])Thorium(?![A-Za-z0-9])")
_GRD_MESSAGE_NAME_RE = re.compile(r'\bname\s*=\s*"([^"]+)"')


def _grit_fingerprint(text):
    """grit extern/FP.FingerPrint: first half of the md5,
    interpreted as a signed 64-bit integer."""
    value = int(hashlib.md5(text.encode("utf-8")).hexdigest()[:16], 16)
    if value & 0x8000000000000000:
        value = -((~value & 0xFFFFFFFFFFFFFFFF) + 1)
    return value


def _grit_message_id(content, meaning=""):
    """grit extern/tclib.GenerateMessageId over the presentable
    content (see _grd_presentable_content), sign bit stripped."""
    fp = _grit_fingerprint(content)
    if meaning:
        fp = (_grit_fingerprint(meaning) + (fp << 1)
              + (1 if fp < 0 else 0))
    return str(fp & 0x7FFFFFFFFFFFFFFF)


def _grd_presentable_content(elem):
    """The string grit fingerprints: the message's text with
    each <ph> element replaced by its upper-cased name,
    leading/trailing whitespace stripped."""
    parts = [elem.text or ""]
    for child in elem:
        if child.tag != "ph":
            raise StageError(
                f"unexpected <{child.tag}> element inside message "
                f"{elem.get('name')!r}; grit messages only carry "
                "<ph> placeholders.")
        parts.append((child.get("name") or "").upper())
        parts.append(child.tail or "")
    return "".join(parts).strip()


def _grd_text_nodes(elem):
    """The message's raw text segments (before/between/after its
    <ph> elements) — where the user-visible words live."""
    nodes = [elem.text or ""]
    for child in elem:
        nodes.append(child.tail or "")
    return nodes


def _grd_if_applies(expr):
    """Evaluate a grit <if expr> for the Windows build. The
    pinned tree's exprs are pure and/or/not combinations of
    defines (verified); anything unparseable is drift and fails
    loudly, while an unknown DEFINE makes the gate pass."""
    if not expr:
        return True
    try:
        return bool(eval(expr, {"__builtins__": {}},
                         dict(LUMO_GRD_WINDOWS_DEFINES)))
    except NameError:
        return True
    except Exception as exc:
        raise StageError(
            f"cannot evaluate grit <if expr={expr!r}>: {exc}")


def _walk_grd_messages(elem, ifs, out):
    """Collect (message element, enclosing <if> exprs) in
    document order.

    GRD conditionals come in two shapes: messages directly
    under <if expr="...">, and <if> wrapping <then>/<else>
    branches. A <then> branch applies when the expr holds; an
    <else> branch applies when it does NOT, so the else side
    inherits the negated expr. (Treating both branches as
    gated by the bare expr would skip every default variant
    written as the else of a branding gate — e.g. the regular
    "About Thorium" is the <else> of the Chrome-for-Testing
    gate in settings_chromium_strings.grdp.)"""
    if elem.tag == "message":
        out.append((elem, ifs))
    for child in elem:
        child_ifs = ifs
        if elem.tag == "if":
            expr = elem.get("expr") or ""
            if expr:
                if child.tag == "else":
                    child_ifs = ifs + (f"not ({expr})",)
                else:
                    child_ifs = ifs + (expr,)
        _walk_grd_messages(child, child_ifs, out)


def _xml_tag_end(text, start, desc):
    """Index of the ">" closing the tag that starts at `start`,
    skipping over quoted attribute values."""
    quote = None
    j = start
    while j < len(text):
        ch = text[j]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == ">":
            return j
        j += 1
    raise StageError(f"{desc}: unterminated tag at offset {start}")


def _grd_message_spans(text, desc):
    """(start, end, body_start) of every <message>...</message>
    in document order, skipping XML comments (which may contain
    commented-out messages). Pairs 1:1 with the elements from
    _walk_grd_messages / ElementTree iteration."""
    spans = []
    i = 0
    n = len(text)
    while i < n:
        comment = text.find("<!--", i)
        match = re.search(r"<message(?=[\s>])", text[i:])
        mpos = i + match.start() if match else -1
        if comment != -1 and (mpos == -1 or comment < mpos):
            end = text.find("-->", comment)
            if end == -1:
                raise StageError(f"{desc}: unterminated XML comment")
            i = end + 3
            continue
        if mpos == -1:
            break
        tag_end = _xml_tag_end(text, mpos, desc)
        close = text.find("</message>", tag_end)
        if close == -1:
            raise StageError(
                f"{desc}: <message> at offset {mpos} has no "
                "</message>; self-closing messages are not "
                "expected in GRD files.")
        spans.append((mpos, close + len("</message>"), tag_end + 1))
        i = close + len("</message>")
    return spans


def _replace_thorium_word_outside_ph(raw, desc):
    """Replace the word Thorium -> Lumo in the text segments of
    a raw message/translation body, leaving <ph>...</ph> element
    spans byte-identical. Returns (new_raw, replacements)."""
    out = []
    count = 0
    i = 0
    while i < len(raw):
        match = re.search(r"<ph(?=[\s/>])", raw[i:])
        if not match:
            seg, k = _THORIUM_WORD_RE.subn("Lumo", raw[i:])
            out.append(seg)
            count += k
            break
        start = i + match.start()
        seg, k = _THORIUM_WORD_RE.subn("Lumo", raw[i:start])
        out.append(seg)
        count += k
        tag_end = _xml_tag_end(raw, start, desc)
        if raw[tag_end - 1] == "/":
            out.append(raw[start:tag_end + 1])
            i = tag_end + 1
        else:
            close = raw.find("</ph>", tag_end)
            if close == -1:
                raise StageError(f"{desc}: unterminated <ph> element")
            out.append(raw[start:close + len("</ph>")])
            i = close + len("</ph>")
    return "".join(out), count


def _lumo_systematic_excluded(name, content, joined_nodes):
    """Audit class B/C protections for the systematic pass."""
    if name in LUMO_SYSTEMATIC_NAME_EXCLUDES:
        return True
    if name.startswith("IDS_LUMO_") or "_LUMO_" in name:
        # Lumo's own strings; several intentionally reference
        # the real Thorium (profile dir, the removed Gemini
        # item's name, the welcome credit's attribution).
        return True
    if name.startswith("IDS_UPDATER_SERVICE_"):
        # Omaha updater strings; no updater ships in Lumo.
        return True
    if "Based on Thorium" in content:
        return True
    if "ThoriumOS" in joined_nodes:
        return True
    if "Not used in Thorium" in joined_nodes:
        # Invisible placeholders kept in sync with the resource
        # maps; not brand display.
        return True
    stripped = content.strip()
    if stripped.endswith(" (Thorium)") or stripped.endswith(" (Lumo)"):
        # Already carries a Labs origin suffix (task 25 pass);
        # the suffix word is data, not a product reference.
        return True
    return False


def _retarget_xtb_entry(text, old_id, new_id, new_body,
                        keep_old, desc):
    """Apply one systematic-pass re-keying to an XTB text.

    Normal case: the entry at old_id moves to new_id with
    new_body. When keep_old is set — an unedited message still
    uses old_id, e.g. an Android/ChromeOS-gated twin with the
    same body (grit ids are content fingerprints, so twins
    share the id AND the translation entry) — the old entry
    STAYS and the new entry is inserted next to it, so neither
    message loses its translation. Returns (new_text, changed).
    Raises StageError when an entry at new_id already exists
    with a different body.
    """
    old_match = re.search(
        r'<translation id="' + re.escape(old_id) + r'">(.*?)'
        r"</translation>", text, re.S)
    if not old_match:
        return text, 0
    new_match = re.search(
        r'<translation id="' + re.escape(new_id) + r'">(.*?)'
        r"</translation>", text, re.S)
    if new_match:
        if new_match.group(1) != new_body:
            raise StageError(
                f"{desc}: translation id {new_id} already "
                "present with a different body; refusing to "
                "overwrite it.")
        if keep_old:
            return text, 0
        # Destination already correct (an identical twin op
        # landed first): just drop the stale old entry.
        return (text[:old_match.start()] + text[old_match.end():]), 1
    entry = (f'<translation id="{new_id}">{new_body}'
             "</translation>")
    if keep_old:
        return (text[:old_match.end()] + "\n" + entry
                + text[old_match.end():]), 1
    return (text[:old_match.start()] + entry
            + text[old_match.end():]), 1


def lumo_systematic_brand_pass(chromium_src):
    """Systematic Thorium -> Lumo replacement over the desktop
    GRD/GRDP files, with the ja XTB re-keying (task 19).

    Returns [(description, count)] like lumo_string_replacements.
    Raises StageError on any drift from the pinned tree's shape.
    """
    id_ops = {}     # old grit id -> (new grit id, new content)
    known_ids = {}  # grit id -> presentable content (pre-edit)
    final_ids = set()  # grit ids in use after the pass
    total_en = 0
    for rel in LUMO_SYSTEMATIC_GRD_FILES:
        path = chromium_src / rel
        if not path.is_file():
            raise StageError(
                f"systematic brand pass: file missing: {path}")
        text = path.read_text(encoding="utf-8")
        try:
            root = ET.fromstring(text.encode("utf-8"))
        except ET.ParseError as exc:
            raise StageError(
                f"systematic brand pass: cannot parse {rel}: {exc}")
        msgs = []
        _walk_grd_messages(root, (), msgs)
        spans = _grd_message_spans(text, rel)
        if len(spans) != len(msgs):
            raise StageError(
                f"systematic brand pass: {rel}: found "
                f"{len(spans)} raw <message> spans for "
                f"{len(msgs)} parsed messages; the file's shape "
                "drifted from what this pass can safely edit.")
        edits = []
        for idx, ((elem, ifs), span) in enumerate(zip(msgs, spans)):
            start, end, body_start = span
            name = elem.get("name") or ""
            seg_name = _GRD_MESSAGE_NAME_RE.search(
                text[start:body_start])
            if not seg_name or seg_name.group(1) != name:
                raise StageError(
                    f"systematic brand pass: {rel}: raw span "
                    f"#{idx} does not pair with parsed message "
                    f"{name!r}; refusing to edit by position.")
            content = _grd_presentable_content(elem)
            meaning = elem.get("meaning") or ""
            old_id = _grit_message_id(content, meaning)
            prev = known_ids.setdefault(old_id, content)
            if prev != content:
                raise StageError(
                    f"systematic brand pass: grit id {old_id} "
                    f"maps to two different messages ({rel}: "
                    f"{name}); refusing to continue.")
            nodes = _grd_text_nodes(elem)
            if not any(_THORIUM_WORD_RE.search(t) for t in nodes):
                final_ids.add(old_id)
                continue
            if _lumo_systematic_excluded(name, content,
                                         "".join(nodes)):
                final_ids.add(old_id)
                continue
            if not all(_grd_if_applies(e) for e in ifs):
                final_ids.add(old_id)
                continue
            body = text[body_start:end - len("</message>")]
            new_body, n = _replace_thorium_word_outside_ph(
                body, f"{rel}: {name}")
            if n == 0:
                raise StageError(
                    f"systematic brand pass: {rel}: {name} was "
                    "selected for replacement but its raw body "
                    "has no replaceable 'Thorium'; the file's "
                    "shape drifted.")
            new_seg = (text[start:body_start] + new_body
                       + "</message>")
            try:
                new_elem = ET.fromstring(new_seg.encode("utf-8"))
            except ET.ParseError as exc:
                raise StageError(
                    f"systematic brand pass: {rel}: {name}: "
                    f"edited message no longer parses: {exc}")
            if any(_THORIUM_WORD_RE.search(t)
                   for t in _grd_text_nodes(new_elem)):
                raise StageError(
                    f"systematic brand pass: {rel}: {name} "
                    "still contains 'Thorium' in a text node "
                    "after replacement.")
            new_content = _grd_presentable_content(new_elem)
            new_id = _grit_message_id(new_content, meaning)
            op = id_ops.setdefault(old_id, (new_id, new_content))
            if op != (new_id, new_content):
                raise StageError(
                    f"systematic brand pass: grit id {old_id} "
                    "would be re-keyed inconsistently.")
            final_ids.add(new_id)
            edits.append((idx, new_seg))
        if edits:
            for idx, new_seg in sorted(edits, reverse=True):
                start, end, _ = spans[idx]
                text = text[:start] + new_seg + text[end:]
            path.write_bytes(text.encode("utf-8"))
            total_en += len(edits)

    # A re-keyed id must not already belong to a DIFFERENT
    # message that stays unchanged (its translation would be
    # stolen). Measured on the pinned tree: no collisions.
    for old_id, (new_id, new_content) in id_ops.items():
        if new_id in known_ids and known_ids[new_id] != new_content:
            raise StageError(
                f"systematic brand pass: new grit id {new_id} "
                "already belongs to a different unchanged "
                "message; refusing to re-key.")

    total_ja = 0
    for rel in LUMO_SYSTEMATIC_JA_XTB_FILES:
        path = chromium_src / rel
        if not path.is_file():
            raise StageError(
                f"systematic brand pass: file missing: {path}")
        text = path.read_text(encoding="utf-8")
        changed = 0
        for old_id, (new_id, _new_content) in id_ops.items():
            if old_id == new_id:
                continue
            match = re.search(
                r'<translation id="' + old_id + r'">(.*?)'
                r"</translation>", text, re.S)
            if not match:
                continue
            new_body, _ = _replace_thorium_word_outside_ph(
                match.group(1), f"{rel}: translation {old_id}")
            text, n = _retarget_xtb_entry(
                text, old_id, new_id, new_body,
                old_id in final_ids,
                f"{rel}: translation {old_id}")
            changed += n
        if changed:
            path.write_bytes(text.encode("utf-8"))
            total_ja += changed

    # Post-pass invariant, checked on the final file state:
    # no message that ships in the Windows build and is not
    # protected may still contain the word in a text node. This
    # is what makes the pass safe to re-run (branding re-runs
    # after a setup interruption): a re-run edits nothing and
    # the invariant already holds, while a pass that silently
    # stopped working on a fresh tree leaves ~800 violations
    # and fails here instead of shipping Thorium strings.
    remaining = 0
    for rel in LUMO_SYSTEMATIC_GRD_FILES:
        path = chromium_src / rel
        text = path.read_text(encoding="utf-8")
        try:
            root = ET.fromstring(text.encode("utf-8"))
        except ET.ParseError as exc:
            raise StageError(
                f"systematic brand pass: post-pass parse of "
                f"{rel} failed: {exc}")
        msgs = []
        _walk_grd_messages(root, (), msgs)
        for elem, ifs in msgs:
            nodes = _grd_text_nodes(elem)
            if not any(_THORIUM_WORD_RE.search(t) for t in nodes):
                continue
            name = elem.get("name") or ""
            content = _grd_presentable_content(elem)
            if _lumo_systematic_excluded(name, content,
                                         "".join(nodes)):
                continue
            if not all(_grd_if_applies(e) for e in ifs):
                continue
            remaining += 1
    if remaining:
        raise StageError(
            f"systematic brand pass: {remaining} unprotected "
            "Windows-build messages still contain 'Thorium' "
            "after the pass; refusing to ship them.")
    return [
        ("systematic GRD pass (Thorium -> Lumo in message text)",
         total_en),
        ("systematic ja XTB re-keying", total_ja),
    ]


# ---------------------------------------------------------------------------
# chrome://flags entries header (task 19, audit A-5).
#
# Thorium's extra flag entries live in a C++ header, not a GRD,
# so the systematic pass cannot see them. The display strings
# (each entry's name and description literals) name Thorium as
# the product; replace the word there too. Protected, as in the
# GRD pass: the "Thorium 2024" experiment name (an occurrence
# immediately followed by " 2024" stays) and Lumo's own entries
# referring to the real Thorium: the glic entry quoting the
# removed item's name ("Open Gemini in Thorium") and the
# Thorium-profile launch entry (patch 0012, task 15), whose name
# and description say "with Thorium Profile" / "the original
# Thorium user profile" — the profile directory belongs to the
# real Thorium, so those two phrasings stay. Flag ids and
# feature identifiers are lowercase / non-literal and are never
# matched.
# ---------------------------------------------------------------------------

_FLAG_ENTRY_WORD_RE = re.compile(
    r"(?<![A-Za-z0-9])(?<!Open Gemini in )(?<!with )"
    r"(?<!original )Thorium(?![A-Za-z0-9])"
    r"(?! 2024)")


def _lumo_flag_translation_entries():
    """Best-effort load of the batch H translation table's
    entries ({} when unavailable). Used by the passes that must
    recognize already-translated literals on a branding re-run;
    lumo_flag_translation_pass itself loads the table strictly
    and raises on any problem."""
    try:
        data = json.loads(
            (Path(__file__).resolve().parent
             / "lumo_flag_translations.json").read_text(
                encoding="utf-8"))
        entries = data["entries"]
        if not isinstance(entries, dict):
            return {}
        return entries
    except (OSError, ValueError, KeyError):
        return {}


def lumo_flag_entries_brand_pass(chromium_src):
    """Thorium -> Lumo in the flags header's display strings.
    Returns [(description, count)]; idempotent (protected
    literals are re-counted as protected on every run)."""
    path = chromium_src / "chrome/browser/thorium_flag_entries.h"
    if not path.is_file():
        raise StageError(f"flag entries pass: file missing: {path}")
    text = path.read_text(encoding="utf-8")
    # On a branding re-run, the batch H translation pass has
    # already rewritten this header's name/description literals
    # to Japanese. Those literals are final text - several keep
    # "Thorium" as a protected proper reference (the Thorium
    # 2024 experiment name, the Thorium-profile launch entry) -
    # and must not be renamed again, so literals whose content
    # is a Japanese translation from the table are skipped
    # (counted as protected when they mention Thorium).
    translated = {
        ent["ja"]
        for key, ent in _lumo_flag_translation_entries().items()
        if key.startswith("chrome/browser/thorium_flag_entries.h|")
        and isinstance(ent, dict) and isinstance(ent.get("ja"), str)
    }
    literal_re = re.compile(r'"(?:[^"\\]|\\.)*"')
    replaced = 0
    protected = 0
    out = []
    last = 0
    for match in literal_re.finditer(text):
        lit = match.group(0)
        if translated and _flag_translation_unquote(lit) in translated:
            if _THORIUM_WORD_RE.search(lit):
                protected += 1
            continue
        if not _THORIUM_WORD_RE.search(lit):
            continue
        new_lit, n = _FLAG_ENTRY_WORD_RE.subn("Lumo", lit)
        if n == 0:
            protected += 1
            continue
        out.append(text[last:match.start()])
        out.append(new_lit)
        last = match.end()
        replaced += n
    if replaced == 0 and protected == 0:
        raise StageError(
            "flag entries pass: no 'Thorium' display strings "
            "found in thorium_flag_entries.h at all; the pinned "
            "header may have drifted.")
    if replaced:
        out.append(text[last:])
        path.write_bytes("".join(out).encode("utf-8"))
    return [("thorium_flag_entries.h display strings", replaced)]


# ---------------------------------------------------------------------------
# Flag display string translation to Japanese (build #24, task 14).
#
# chrome://flags shows the flag names and descriptions straight
# from C++ string constants - they are NOT XTB resources (the
# flag_descriptions.h header itself says "These messages are not
# translated"). The full set of user-visible flag strings in the
# pinned tree (4,074 strings, enumerated mechanically):
#
#   chrome/browser/flag_descriptions.h            3,156 constants
#       inline constexpr char kXxx[] inside
#       namespace flag_descriptions (Name / Description /
#       choice labels).
#   components/commerce/core/flag_descriptions.cc      12
#   components/enterprise/net/core/flag_descriptions.cc  4
#   components/contextual_tasks/public/features.cc     48
#   components/omnibox/common/omnibox_features.cc       2
#   chrome/browser/ui/tabs/tab_group_home/constants.cc  2
#       (const char definitions inside namespace
#       flag_descriptions; the matching headers hold only
#       extern declarations)
#   chrome/browser/thorium_flag_entries.h
#       75 entries x (name, description) = 150 literal fields.
#   chrome/browser/about_flags.cc
#       700 FeatureVariation / Choice element labels (the first
#       literal of each element; elements whose label is a
#       constant reference draw from the sources above).
#
# A patch file rewriting these would run to ~9,000 lines for
# flag_descriptions.h alone - unreviewable and fragile - so the
# translations live in a data table instead:
# scripts/ci/lumo_flag_translations.json maps
# "<relpath>|<identifier>" -> {"en": ..., "ja": ...}, where the
# identifier is stable (constant name / flag id + field /
# variation array + index) and never the English text itself.
# This pass rewrites each literal group in place, keyed by
# identifier, with a three-way check per string:
#   current == en  -> replace with the Japanese literal;
#   current == ja  -> already translated, skip;
#   anything else  -> StageError (the pinned tree drifted).
# A mapping key the tree does not contain, and a tree identifier
# the mapping does not cover, are likewise StageErrors, so full
# coverage and idempotency hold by construction. Runs AFTER
# lumo_flag_entries_brand_pass: the table's "en" values for
# thorium_flag_entries.h are the post-brand-pass strings (product
# name already "Lumo"), which is the state this pass sees.
# A replaced literal group becomes a single UTF-8 literal
# (precedent: the smart quotes already present in
# flag_descriptions.h); only backslash and double-quote are
# escaped, and a translation containing a control character is
# rejected at load time.
# ---------------------------------------------------------------------------

_FLAG_TRANSLATION_STR = r'"(?:[^"\\]|\\.)*"'
_FLAG_TRANSLATION_LITERAL_RE = re.compile(_FLAG_TRANSLATION_STR)
_FLAG_TRANSLATION_CONST_DEF_RE = re.compile(
    r"(?:inline\s+constexpr|const)\s+char\s+(k\w+)\[\]\s*=\s*"
    r"((?:" + _FLAG_TRANSLATION_STR + r"\s*)+);", re.S)

# (relpath, kind) pairs; kind selects the extractor below.
_FLAG_TRANSLATION_SOURCES = (
    ("chrome/browser/flag_descriptions.h", "const"),
    ("components/commerce/core/flag_descriptions.cc", "const"),
    ("components/enterprise/net/core/flag_descriptions.cc", "const"),
    ("components/contextual_tasks/public/features.cc", "const"),
    ("components/omnibox/common/omnibox_features.cc", "const"),
    ("chrome/browser/ui/tabs/tab_group_home/constants.cc", "const"),
    ("chrome/browser/thorium_flag_entries.h", "thorium"),
    ("chrome/browser/about_flags.cc", "variation"),
)


def _flag_translation_unquote(lit):
    body = lit[1:-1]
    out = []
    i = 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            n = body[i + 1]
            out.append({"n": "\n", "t": "\t", "r": "\r", "\\": "\\",
                        '"': '"', "'": "'", "0": "\0"}.get(n, "\\" + n))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _flag_translation_join(text):
    return "".join(
        _flag_translation_unquote(m.group(0))
        for m in _FLAG_TRANSLATION_LITERAL_RE.finditer(text))


def _flag_translation_brace_span(text, open_idx):
    """(body_start, body_end, close_idx) for the brace opened at
    open_idx, skipping string literals and // comments."""
    depth = 0
    i = open_idx
    while i < len(text):
        c = text[i]
        if c == '"':
            m = _FLAG_TRANSLATION_LITERAL_RE.match(text, i)
            i = m.end() if m else i + 1
            continue
        if c == "/" and i + 1 < len(text) and text[i + 1] == "/":
            j = text.find("\n", i)
            i = len(text) if j < 0 else j
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return open_idx + 1, i, i
        i += 1
    raise StageError("flag translation pass: unbalanced braces")


def _flag_translation_namespace_bodies(text):
    """(start, end) body spans of `namespace flag_descriptions`
    blocks."""
    spans = []
    for m in re.finditer(
            r"namespace\s+(?:\w+::)?flag_descriptions\s*\{", text):
        start, end, _ = _flag_translation_brace_span(text, m.end() - 1)
        spans.append((start, end))
    return spans


def _flag_translation_const_spans(text):
    """{constant name: (start, end, value)} for the flag string
    constants defined inside namespace flag_descriptions."""
    out = {}
    for start, end in _flag_translation_namespace_bodies(text):
        for m in _FLAG_TRANSLATION_CONST_DEF_RE.finditer(
                text, start, end):
            out[m.group(1)] = (
                m.start(2), m.end(2),
                _flag_translation_join(m.group(2)))
    return out


_FLAG_TRANSLATION_LITERAL_GROUP_RE = re.compile(
    r"\s*((?:" + _FLAG_TRANSLATION_STR + r"\s*)+),")
_FLAG_TRANSLATION_ENTRY_HEAD_RE = re.compile(
    r'\{\s*"([a-z0-9][a-z0-9\-]*)"\s*,')


def _flag_translation_thorium_spans(text):
    """{"<flag id>:name"/"<flag id>:description": (start, end,
    value)} for thorium_flag_entries.h entries."""
    out = {}
    for m in _FLAG_TRANSLATION_ENTRY_HEAD_RE.finditer(text):
        flag_id = m.group(1)
        nm = _FLAG_TRANSLATION_LITERAL_GROUP_RE.match(text, m.end())
        if not nm:
            continue
        dm = _FLAG_TRANSLATION_LITERAL_GROUP_RE.match(text, nm.end())
        if not dm:
            continue
        out[f"{flag_id}:name"] = (
            nm.start(1), nm.end(1),
            _flag_translation_join(nm.group(1)))
        out[f"{flag_id}:description"] = (
            dm.start(1), dm.end(1),
            _flag_translation_join(dm.group(1)))
    return out


def _flag_translation_variation_spans(text):
    """{"<array>[<index>]": (start, end, value)} for the first
    literal of each FeatureVariation / Choice element in
    about_flags.cc."""
    out = {}
    for am in re.finditer(
            r"(?:FeatureVariation|Choice)\s+(k\w+)\[\]\s*=\s*\{",
            text):
        arr = am.group(1)
        body_start, body_end, _ = _flag_translation_brace_span(
            text, am.end() - 1)
        idx = 0
        i = body_start
        while i < body_end:
            if text[i] == "{":
                es, ee, close = _flag_translation_brace_span(text, i)
                m = re.match(
                    r"\s*((?:" + _FLAG_TRANSLATION_STR + r"\s*)+)",
                    text[es:ee])
                if m:
                    out[f"{arr}[{idx}]"] = (
                        es + m.start(1), es + m.end(1),
                        _flag_translation_join(m.group(1)))
                idx += 1
                i = close + 1
            elif text[i] == '"':
                m = _FLAG_TRANSLATION_LITERAL_RE.match(text, i)
                i = m.end() if m else i + 1
            else:
                i += 1
    return out


_FLAG_TRANSLATION_EXTRACTORS = {
    "const": _flag_translation_const_spans,
    "thorium": _flag_translation_thorium_spans,
    "variation": _flag_translation_variation_spans,
}


def _flag_translation_escape(value):
    return '"' + value.replace("\\", "\\\\").replace(
        '"', '\\"') + '"'


def lumo_flag_translation_pass(chromium_src, mapping_path=None):
    """Translate every chrome://flags display string to Japanese
    (task 14), driven by scripts/ci/lumo_flag_translations.json.
    Returns [(description, count)]; idempotent (a second run
    replaces nothing and verifies every string is already the
    Japanese text)."""
    if mapping_path is None:
        mapping_path = (Path(__file__).resolve().parent
                        / "lumo_flag_translations.json")
    try:
        data = json.loads(
            Path(mapping_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StageError(
            f"flag translation pass: cannot load the translation "
            f"table {mapping_path}: {exc}")
    if data.get("version") != 1 or not isinstance(
            data.get("entries"), dict):
        raise StageError(
            "flag translation pass: the translation table has an "
            "unexpected shape (expected {'version': 1, 'entries': "
            "{...}})")
    entries = data["entries"]
    for key, ent in entries.items():
        if (not isinstance(ent, dict)
                or not isinstance(ent.get("en"), str)
                or not isinstance(ent.get("ja"), str)
                or any(ord(c) < 0x20 for c in ent["ja"])):
            raise StageError(
                f"flag translation pass: malformed entry for "
                f"{key!r} in the translation table")

    found = {}
    texts = {}
    for rel, kind in _FLAG_TRANSLATION_SOURCES:
        path = chromium_src / rel
        if not path.is_file():
            raise StageError(
                f"flag translation pass: file missing: {path}")
        text = path.read_text(encoding="utf-8")
        texts[rel] = text
        for ident, span in _FLAG_TRANSLATION_EXTRACTORS[kind](
                text).items():
            found[f"{rel}|{ident}"] = (rel,) + span
    missing = sorted(set(entries) - set(found))
    if missing:
        raise StageError(
            f"flag translation pass: {len(missing)} translation "
            f"table key(s) not found in the tree (first: "
            f"{missing[0]}); the pinned tree may have drifted.")
    uncovered = sorted(set(found) - set(entries))
    if uncovered:
        raise StageError(
            f"flag translation pass: {len(uncovered)} flag "
            f"string(s) in the tree have no translation (first: "
            f"{uncovered[0]}); refusing to leave English flag "
            f"strings behind.")

    replacements = {}
    for key, ent in entries.items():
        rel, start, end, current = found[key]
        if current == ent["ja"]:
            continue
        if current != ent["en"]:
            raise StageError(
                f"flag translation pass: {key} holds neither the "
                f"expected English nor the Japanese text; the "
                f"pinned tree may have drifted.")
        replacements.setdefault(rel, []).append(
            (start, end, _flag_translation_escape(ent["ja"])))
    total = 0
    for rel, reps in replacements.items():
        text = texts[rel]
        for start, end, new in sorted(reps, reverse=True):
            text = text[:start] + new + text[end:]
        (chromium_src / rel).write_bytes(text.encode("utf-8"))
        total += len(reps)
    return [("flag display strings translated to Japanese", total)]


# ---------------------------------------------------------------------------
# Labs origin suffixes (build #24, task 25).
#
# Every experiment listed in the Chrome Labs bubble gets its
# origin appended to its NAME: " (Thorium)" for experiments that
# came from Thorium, " (Lumo)" for experiments Lumo added. The
# panel names are the IDS_CHROMELABS_*_NAME strings registered
# in chrome/browser/ui/toolbar/chrome_labs/chrome_labs_model.cc.
# Origin table (all ten entries shown in the bubble):
#
#   IDS_CHROMELABS_THORIUM_2024_NAME            Thorium
#       Thorium's own Th24 UI experiment (patch series +
#       thorium_flag_entries.h "thorium-2024").
#   IDS_CHROMELABS_RESTORE_TAB_BUTTON_NAME      Thorium
#       Thorium patch other/restore-tab-button.patch.
#   IDS_CHROMELABS_MIDDLE_CLICK_AUTOSCROLL_NAME Thorium
#       Thorium's middle-click autoscroll experiment.
#   IDS_CHROMELABS_LUMO_GLIC_MENU_NAME          Lumo (patch 0006)
#   IDS_CHROMELABS_LUMO_LAUNCH_THORIUM_PROFILE_NAME
#                                               Lumo (patch 0012;
#       the experiment that un-hides patch 0007's Thorium-profile
#       launch menu item, task 15)
#   IDS_CHROMELABS_LUMO_HOVER_CARD_MEMORY_USAGE_NAME
#                                               Lumo (patch 0011)
#   IDS_CHROMELABS_LUMO_DARK_MODE_SCHEDULE_NAME Lumo (patch 0011)
#   IDS_CHROMELABS_LUMO_TRACKING_PROTECTION_STRICT_NAME
#                                               Lumo (patch 0011)
#   IDS_CHROMELABS_LUMO_FULL_PAGE_SCREENSHOT_NAME
#                                               Lumo (patch 0011)
#   IDS_CHROMELABS_LUMO_TAB_GROUP_SESSION_SAVE_NAME
#                                               Lumo (patch 0011)
#
# The Thorium entries carry real ja translations, which are
# re-keyed to the suffixed body's grit id with the suffix
# appended to the translation as well (the user sees e.g.
# "Thorium 2024 UI を有効にする (Thorium)"). The Lumo entries'
# GRD bodies are already Japanese and have no ja XTB entries,
# so only the GRD body changes. Runs AFTER the systematic pass:
# the suffixed bodies are protected from it on re-runs by the
# origin-suffix exclusion in _lumo_systematic_excluded (and the
# Th24 name by its name exclusion).
# ---------------------------------------------------------------------------

LUMO_LABS_ORIGIN_SUFFIXES = (
    ("IDS_CHROMELABS_THORIUM_2024_NAME", " (Thorium)"),
    ("IDS_CHROMELABS_RESTORE_TAB_BUTTON_NAME", " (Thorium)"),
    ("IDS_CHROMELABS_MIDDLE_CLICK_AUTOSCROLL_NAME", " (Thorium)"),
    ("IDS_CHROMELABS_LUMO_GLIC_MENU_NAME", " (Lumo)"),
    ("IDS_CHROMELABS_LUMO_LAUNCH_THORIUM_PROFILE_NAME", " (Lumo)"),
    ("IDS_CHROMELABS_LUMO_HOVER_CARD_MEMORY_USAGE_NAME", " (Lumo)"),
    ("IDS_CHROMELABS_LUMO_DARK_MODE_SCHEDULE_NAME", " (Lumo)"),
    ("IDS_CHROMELABS_LUMO_TRACKING_PROTECTION_STRICT_NAME",
     " (Lumo)"),
    ("IDS_CHROMELABS_LUMO_FULL_PAGE_SCREENSHOT_NAME", " (Lumo)"),
    ("IDS_CHROMELABS_LUMO_TAB_GROUP_SESSION_SAVE_NAME", " (Lumo)"),
)


def lumo_labs_origin_suffix_pass(chromium_src):
    """Append the origin suffix to every Labs experiment name
    (task 25). Returns [(description, count)]; idempotent."""
    grd_rel = "chrome/app/generated_resources.grd"
    xtb_rel = "chrome/app/resources/generated_resources_ja.xtb"
    grd_path = chromium_src / grd_rel
    xtb_path = chromium_src / xtb_rel
    if not grd_path.is_file() or not xtb_path.is_file():
        raise StageError(
            f"Labs origin suffix pass: file missing: "
            f"{grd_path} / {xtb_path}")
    grd_text = grd_path.read_text(encoding="utf-8")
    xtb_text = xtb_path.read_text(encoding="utf-8")
    try:
        root = ET.fromstring(grd_text.encode("utf-8"))
    except ET.ParseError as exc:
        raise StageError(
            f"Labs origin suffix pass: cannot parse {grd_rel}: "
            f"{exc}")
    by_name = {}
    for elem in root.iter("message"):
        by_name.setdefault(elem.get("name") or "", []).append(elem)
    spans = _grd_message_spans(grd_text, grd_rel)
    span_by_name = {}
    for start, end, body_start in spans:
        seg_name = _GRD_MESSAGE_NAME_RE.search(
            grd_text[start:body_start])
        if seg_name:
            span_by_name[seg_name.group(1)] = (start, end,
                                               body_start)

    grd_edits = []
    ja_ops = []  # (old_id, new_id, suffix)
    applied = 0
    for name, suffix in LUMO_LABS_ORIGIN_SUFFIXES:
        elems = by_name.get(name)
        if not elems or len(elems) != 1:
            raise StageError(
                f"Labs origin suffix pass: expected exactly one "
                f"{name} in {grd_rel}, found "
                f"{len(elems) if elems else 0}.")
        elem = elems[0]
        if len(elem):
            raise StageError(
                f"Labs origin suffix pass: {name} unexpectedly "
                "contains placeholder elements; the suffix "
                "append assumes a plain-text name.")
        content = _grd_presentable_content(elem)
        meaning = elem.get("meaning") or ""
        if content.endswith(suffix):
            # Already applied; the ja entry (if any) must sit at
            # THIS body's id and carry the suffix too.
            cur_id = _grit_message_id(content, meaning)
            match = re.search(
                r'<translation id="' + cur_id + r'">(.*?)'
                r"</translation>", xtb_text, re.S)
            if match and not match.group(1).endswith(suffix):
                raise StageError(
                    f"Labs origin suffix pass: {name} is "
                    "suffixed in the GRD but its ja translation "
                    "is not; inconsistent state.")
            applied += 1
            continue
        old_id = _grit_message_id(content, meaning)
        new_id = _grit_message_id(content + suffix, meaning)
        if name not in span_by_name:
            raise StageError(
                f"Labs origin suffix pass: no raw span found "
                f"for {name} in {grd_rel}.")
        start, end, body_start = span_by_name[name]
        body = grd_text[body_start:end - len("</message>")]
        stripped = body.strip()
        if stripped != content:
            raise StageError(
                f"Labs origin suffix pass: {name}: raw body "
                f"{stripped!r} does not match the parsed "
                f"content {content!r}.")
        new_body = body.replace(stripped, stripped + suffix, 1)
        grd_edits.append(
            (start, end, grd_text[start:body_start] + new_body
             + "</message>"))
        ja_ops.append((old_id, new_id, suffix))
        applied += 1

    if applied != len(LUMO_LABS_ORIGIN_SUFFIXES):
        raise StageError(
            f"Labs origin suffix pass: handled {applied} of "
            f"{len(LUMO_LABS_ORIGIN_SUFFIXES)} Labs names.")
    for start, end, new_seg in sorted(grd_edits, reverse=True):
        grd_text = grd_text[:start] + new_seg + grd_text[end:]
    if grd_edits:
        grd_path.write_bytes(grd_text.encode("utf-8"))

    ja_changed = 0
    for old_id, new_id, suffix in ja_ops:
        match = re.search(
            r'<translation id="' + old_id + r'">(.*?)'
            r"</translation>", xtb_text, re.S)
        if not match:
            continue  # the Lumo names have no ja entry by design
        if re.search(r'<translation id="' + new_id + r'">',
                     xtb_text):
            raise StageError(
                f"Labs origin suffix pass: translation id "
                f"{new_id} already present in {xtb_rel}.")
        ja_body = match.group(1)
        if ja_body.endswith(suffix):
            raise StageError(
                f"Labs origin suffix pass: ja translation "
                f"{old_id} already carries the suffix but sits "
                "at the old id; inconsistent state.")
        xtb_text = (xtb_text[:match.start()]
                    + f'<translation id="{new_id}">'
                    f"{ja_body}{suffix}</translation>"
                    + xtb_text[match.end():])
        ja_changed += 1
    if ja_ops and ja_changed:
        xtb_path.write_bytes(xtb_text.encode("utf-8"))
    return [
        ("Labs origin suffixes (GRD names)", len(grd_edits)),
        ("Labs origin suffixes (ja re-keying)", ja_changed),
    ]


def _labs_suffixed_patch_variant(data):
    """Variant of a Lumo patch whose Labs-name GRD bodies carry
    the task 25 origin suffixes; for reverse checks only.

    lumo_labs_origin_suffix_pass (which runs AFTER the patches
    in apply_lumo_branding) appends " (Thorium)" / " (Lumo)" to
    the GRD bodies of the Labs experiment names — several of
    which were added by Lumo patches themselves (0006, 0011,
    0012). On a branding re-run, those patches' post-images
    therefore differ from the patch files by exactly the
    suffix, and both reverse checks fail even though the
    patches are fully applied (found in build #24 batch B
    verification: the second branding pass on a completed tree
    raised StageError at patch 0006). This variant rewrites
    the affected added body lines to their suffixed form so
    the reverse checks can recognize the applied state.
    Returns None when the patch adds no Labs-name body (the
    common case). The FORWARD path always uses the original
    patch: the suffix is appended by the pass afterwards.
    """
    suffix_by_name = dict(LUMO_LABS_ORIGIN_SUFFIXES)
    lines = data.decode("utf-8").split("\n")
    changed = False
    in_grd = False
    pending = None
    for i, line in enumerate(lines):
        if line.startswith("diff --git "):
            in_grd = line.rstrip().endswith(
                "b/chrome/app/generated_resources.grd")
            pending = None
            continue
        if line.startswith("@@"):
            pending = None
            continue
        if not in_grd:
            continue
        if line.startswith("+"):
            match = re.search(r'<message name="([A-Z0-9_]+)"',
                              line)
            if match and match.group(1) in suffix_by_name:
                pending = suffix_by_name[match.group(1)]
                continue
            if pending is not None:
                # The first added line after the <message ...>
                # tag is the body (Labs names are single-line
                # plain text; an immediate </message> would be
                # an empty body, which does not occur).
                if "</message>" not in line \
                        and not line.endswith(pending):
                    lines[i] = line + pending
                    changed = True
                pending = None
        else:
            pending = None
    if not changed:
        return None
    return "\n".join(lines).encode("utf-8")


def _flag_translated_patch_variant(data):
    """Variant of a Lumo patch whose thorium_flag_entries.h
    entry literals carry the batch H Japanese translations; for
    reverse checks only.

    Patches 0006, 0011 and 0012 add Lumo's own flag entries to
    thorium_flag_entries.h with English name/description
    literals. lumo_flag_translation_pass (which runs AFTER the
    patches in apply_lumo_branding) rewrites those literals to
    the Japanese texts, so on a branding re-run the three
    patches' post-images differ from the patch files and both
    reverse checks fail even though the patches are fully
    applied (found in build #24 batch H verification: the
    second branding pass on a completed tree raised StageError
    at patch 0006). This variant rewrites the ADDED entry
    blocks to their translated form - keyed by flag id from
    the translation table, so it tracks the table exactly -
    and recomputes the hunk headers' new-side line numbers.
    Context lines are left alone (the -C0 reverse check does
    not need them). Returns None when the patch adds no
    translated flag entry. The FORWARD path always uses the
    original patch: the translation is applied by the pass
    afterwards.
    """
    entries = _lumo_flag_translation_entries()
    if not entries:
        return None
    prefix = "chrome/browser/thorium_flag_entries.h|"
    ja_by_id = {}
    for key, ent in entries.items():
        if key.startswith(prefix) and ":" in key[len(prefix):]:
            flag_id, field = key[len(prefix):].rsplit(":", 1)
            if isinstance(ent, dict) and isinstance(
                    ent.get("ja"), str):
                ja_by_id.setdefault(flag_id, {})[field] = ent["ja"]
    if not ja_by_id:
        return None

    lines = data.decode("utf-8").split("\n")
    out = []
    in_target = False
    changed = False
    shift = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            in_target = line.rstrip().endswith(
                "b/chrome/browser/thorium_flag_entries.h")
            shift = 0
            out.append(line)
            i += 1
            continue
        if not in_target or not line.startswith("@@"):
            out.append(line)
            i += 1
            continue
        # One hunk: rewrite its added entry blocks, then fix the
        # header's new-side start/count by the added-line delta
        # plus the cumulative shift from earlier hunks of this
        # file section.
        header = re.match(
            r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)", line)
        body = []
        i += 1
        while i < len(lines) and not lines[i].startswith("@@") \
                and not lines[i].startswith("diff --git "):
            body.append(lines[i])
            i += 1
        new_body, delta, rewrote = _flag_translated_hunk_body(
            body, ja_by_id)
        if rewrote:
            changed = True
        if delta:
            old_start = int(header.group(3))
            old_count = int(header.group(4) or "1")
            line = (f"@@ -{header.group(1)}"
                    f"{',' + header.group(2) if header.group(2) else ''}"
                    f" +{old_start + shift},{old_count + delta} @@"
                    f"{header.group(5)}")
            shift += delta
        out.append(line)
        out.extend(new_body)
    if not changed:
        return None
    return "\n".join(out).encode("utf-8")


def _flag_translated_hunk_body(body, ja_by_id):
    """Rewrite added flag-entry blocks in one hunk body to
    their translated form. Returns (new_body, added_line_delta,
    rewrote_any)."""
    out = []
    delta = 0
    rewrote = False
    i = 0
    head_re = re.compile(r'\{"([a-z0-9][a-z0-9\-]*)",$')
    while i < len(body):
        line = body[i]
        match = head_re.search(line[1:]) if line.startswith("+") \
            else None
        if match and match.group(1) in ja_by_id:
            # Collect the entry's added lines through the tail
            # line ending in "},".
            block = [line]
            j = i + 1
            while j < len(body) and body[j].startswith("+") \
                    and not block[-1][1:].rstrip().endswith("},"):
                block.append(body[j])
                j += 1
            if block[-1][1:].rstrip().endswith("},"):
                text = "\n".join(b[1:] for b in block)
                spans = _flag_translation_thorium_spans(text)
                fields = ja_by_id[match.group(1)]
                name_key = f"{match.group(1)}:name"
                desc_key = f"{match.group(1)}:description"
                if name_key in spans and desc_key in spans \
                        and "name" in fields \
                        and "description" in fields:
                    for key, field in ((desc_key, "description"),
                                       (name_key, "name")):
                        start, end, _val = spans[key]
                        text = (text[:start]
                                + _flag_translation_escape(
                                    fields[field])
                                + text[end:])
                    new_block = ["+" + part
                                 for part in text.split("\n")]
                    delta += len(new_block) - len(block)
                    rewrote = True
                    out.extend(new_block)
                    i = j
                    continue
            out.extend(block)
            i = j
            continue
        out.append(line)
        i += 1
    return out, delta, rewrote


def _git_apply_both_ways(git, chromium_src, patch_path, env, deadline):
    """git apply with a both-ways idempotency check.

    The REVERSE check runs first: a patch whose result is already
    in the tree is skipped, even if its context would also match
    somewhere else in the forward direction (a small hunk with
    common context lines, e.g. a CSS rule appended after a repeated
    pattern, could otherwise be applied twice at a wrong offset).

    Batch 2 (patches 0006-0011) made several Lumo patches insert
    lines into adjacent regions of the same files (the Lumo
    string block in generated_resources.grd, the Lumo feature
    block in ui_features.h/.cc, the flag-entry and Labs blocks).
    Once a later patch has inserted lines inside an earlier
    patch's context window, the earlier patch no longer
    reverse-checks with full context even though its result is
    fully present. The reverse check therefore falls back to a
    zero-context (-C0) check, which only requires each hunk's
    post-image lines to be present contiguously. The added lines
    are Lumo-unique (string ids, feature names), so a false
    positive would require that exact content to already exist
    in the pristine tree. The forward path is unchanged.

    A third fallback covers the task 25 origin suffix pass,
    which edits the GRD bodies of Labs names that Lumo patches
    themselves added: the reverse checks are retried against a
    suffix-carrying variant of the patch (see
    _labs_suffixed_patch_variant).

    A fourth fallback covers the batch H flag translation pass,
    which rewrites the flag-entry literals that patches 0006,
    0011 and 0012 added to thorium_flag_entries.h: the reverse
    checks are retried against a translation-carrying variant
    of the patch, alone and combined with the suffix variant
    (see _flag_translated_patch_variant).

    A patch file with CRLF line endings can never apply to the LF
    tree (git apply matches context byte-for-byte), so it is
    repaired first and the LF-normalized copy is applied instead.
    This is not theoretical: build #14 failed at
    0001-lumo-install-static.patch with "patch does not apply"
    although the patch content was exact against the real
    post-Thorium tree, because the Windows checkout had rewritten
    the repo's patch files to CRLF (the Lumo repo had no
    .gitattributes pinning *.patch to eol=lf, unlike Thorium's).
    The repo now pins that rule; this normalization is the second
    line of defense for any future ending-mangling path.
    """
    rel = patch_path.name
    patch_arg = patch_path
    temp_dir = None
    data = patch_path.read_bytes()
    if b"\r\n" in data:
        temp_dir = tempfile.TemporaryDirectory(
            prefix="lumo-patch-lf-")
        patch_arg = Path(temp_dir.name) / rel
        patch_arg.write_bytes(data.replace(b"\r\n", b"\n"))
        log(f"Lumo branding: {rel} has CRLF line endings; "
            "applying an LF-normalized copy instead (a CRLF "
            "patch cannot apply to the LF tree).")
    try:
        code = run(
            [git, "apply", "--check", "--reverse", str(patch_arg)],
            chromium_src, env=env, timeout_at=deadline)
        if code == 0:
            log(f"Lumo branding: {rel} already applied; skipping.")
            return False
        code = run(
            [git, "apply", "--check", "--reverse", "-C0",
             str(patch_arg)],
            chromium_src, env=env, timeout_at=deadline)
        if code == 0:
            log(f"Lumo branding: {rel} already applied "
                "(zero-context reverse check); skipping.")
            return False
        variant = _labs_suffixed_patch_variant(
            patch_arg.read_bytes())
        if variant is not None:
            if temp_dir is None:
                temp_dir = tempfile.TemporaryDirectory(
                    prefix="lumo-patch-suffix-")
            variant_path = Path(temp_dir.name) / (rel + ".variant")
            variant_path.write_bytes(variant)
            for extra in ([], ["-C0"]):
                code = run(
                    [git, "apply", "--check", "--reverse",
                     *extra, str(variant_path)],
                    chromium_src, env=env, timeout_at=deadline)
                if code == 0:
                    log(f"Lumo branding: {rel} already applied "
                        "(the Labs origin suffix pass has since "
                        "suffixed its Labs names); skipping.")
                    return False
        translated = _flag_translated_patch_variant(
            patch_arg.read_bytes())
        if translated is not None:
            # The three patches this variant exists for also
            # carry Labs names, so try the combined form (Labs
            # suffixes on top of the flag translations) as well
            # as the translated form alone.
            combined = _labs_suffixed_patch_variant(translated)
            candidates = [combined, translated] \
                if combined is not None else [translated]
            if temp_dir is None:
                temp_dir = tempfile.TemporaryDirectory(
                    prefix="lumo-patch-translated-")
            for idx, cand in enumerate(candidates):
                cand_path = Path(temp_dir.name) / (
                    rel + f".translated{idx}")
                cand_path.write_bytes(cand)
                for extra in ([], ["-C0"]):
                    code = run(
                        [git, "apply", "--check", "--reverse",
                         *extra, str(cand_path)],
                        chromium_src, env=env, timeout_at=deadline)
                    if code == 0:
                        log(f"Lumo branding: {rel} already "
                            "applied (the flag translation pass "
                            "has since translated its flag "
                            "entries); skipping.")
                        return False
        code = run(
            [git, "apply", "--check", str(patch_arg)],
            chromium_src, env=env, timeout_at=deadline)
        if code == 0:
            run_checked(
                [git, "apply", str(patch_arg)],
                chromium_src, env=env, timeout_at=deadline)
            log(f"Lumo branding: applied {rel}.")
            return True
        raise StageError(
            f"Lumo branding patch {rel} neither applies nor is "
            "already applied; the post-Thorium tree may have "
            "drifted.")
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


# Startup password lock (task 21, patch 0019). The verification
# data comes from the LUMO_LOCK_HASH GitHub Secret in the form
# "<salt_hex>:<hash_hex>" (16-byte salt; hash = SHA-256 over the
# salt bytes followed by the password's UTF-8 bytes) and reaches
# the browser through a generated header that patch 0019 includes
# via __has_include. The header is deliberately NOT part of patch
# 0019: stage.py rewrites it on every branding run (the secret can
# be rotated, and a missing secret must be able to turn the lock
# back off), which a patch-owned file could not survive. Only the
# salt and the hash ever reach the tree or the binary; the password
# itself never leaves the owner's machine. The optional second
# password (LUMO_LOCK_HASH_2, build #30; either password unlocks)
# gets its own generated header, lumo_lock_config_2.h, so a tree
# set up before the second password existed carries only the
# first header and still builds (patch 0019 reads the second
# header through __has_include).
LUMO_LOCK_CONFIG_REL_PATH = "chrome/browser/lumo/lumo_lock_config.h"
LUMO_LOCK_CONFIG_2_REL_PATH = "chrome/browser/lumo/lumo_lock_config_2.h"
LUMO_LOCK_HASH_PATTERN = re.compile(r"^([0-9a-f]{32}):([0-9a-f]{64})$")


def _parse_lumo_lock_hash(raw, secret_name):
    """Validate one "<salt_hex>:<hash_hex>" lock secret value.

    Returns (salt_hex, hash_hex), or ("", "") when the secret is
    unset. A set but malformed value is a loud StageError, never a
    quietly wrong build; the value is never printed to the log.
    """
    if not raw:
        return "", ""
    match = LUMO_LOCK_HASH_PATTERN.match(raw)
    if match is None:
        raise StageError(
            f"{secret_name} is set but malformed: expected "
            "'<salt_hex>:<hash_hex>' with a 16-byte salt (32 "
            "lowercase hex characters) and a SHA-256 hash (64 "
            "lowercase hex characters); refusing to build a "
            "lock with an unverified value (the value itself "
            "is not shown)"
        )
    return match.group(1), match.group(2)


def _write_lumo_lock_header(chromium_src, rel_path, guard,
                            salt_const, hash_const, salt_hex,
                            hash_hex, secret_name):
    """Write one generated lock config header, then read it back
    and verify it byte-for-byte."""
    lines = [
        f"// Generated by scripts/ci/stage.py from the {secret_name}",
        "// secret at build time. Do not edit; regenerated on every",
        "// branding run. Contains only the salt and the expected",
        "// SHA-256 hash - never the password itself.",
        f"#ifndef {guard}",
        f"#define {guard}",
        "",
        "namespace lumo {",
        "",
        f'inline constexpr char {salt_const}[] = "{salt_hex}";',
        f'inline constexpr char {hash_const}[] = "{hash_hex}";',
        "",
        "}  // namespace lumo",
        "",
        f"#endif  // {guard}",
        "",
    ]
    text = "\n".join(lines)
    header_path = chromium_src / rel_path
    header_path.parent.mkdir(parents=True, exist_ok=True)
    header_path.write_text(text, encoding="utf-8")
    written = header_path.read_text(encoding="utf-8")
    if written != text:
        raise StageError(
            f"{rel_path} verification failed: the written "
            "file does not match the generated content; refusing "
            "to build with an unverified lock configuration"
        )


def write_lumo_lock_config(chromium_src, environ=None):
    """Generate chrome/browser/lumo/lumo_lock_config.h from the
    LUMO_LOCK_HASH environment variable (a GitHub Secret), and
    chrome/browser/lumo/lumo_lock_config_2.h from the optional
    LUMO_LOCK_HASH_2 secret (a second password; either password
    unlocks, patch 0019).

    Mirrors the API key injection in write_args_gn: a set but
    malformed value is a loud StageError, never a quietly wrong
    build; the value is never printed to the log; the written file
    is read back and verified byte-for-byte. An UNSET secret is not
    an error: like the signing step's transition behavior, the
    build proceeds with the lock disabled (an empty-hash header),
    so a build without the secret — someone else's fork, a lost
    secret — still produces a working browser instead of one locked
    with an unknown password.

    The second password gets its OWN header, not new constants in
    the first one: a tree set up before the second password
    existed carries only the first header, and patch 0019 treats
    a missing second header as "no second password" via
    __has_include, so such a tree still builds and locks with
    the first password alone.
    """
    if environ is None:
        environ = os.environ
    raw = (environ.get("LUMO_LOCK_HASH") or "").strip()
    salt_hex, hash_hex = _parse_lumo_lock_hash(raw, "LUMO_LOCK_HASH")
    if not raw:
        log("Lumo startup lock: LUMO_LOCK_HASH is not set; "
            "building with the startup password lock DISABLED.")
    _write_lumo_lock_header(
        chromium_src, LUMO_LOCK_CONFIG_REL_PATH,
        "CHROME_BROWSER_LUMO_LUMO_LOCK_CONFIG_H_",
        "kLockSaltHex", "kLockHashHex", salt_hex, hash_hex,
        "LUMO_LOCK_HASH")
    if raw:
        log("Lumo startup lock: lock config header written from "
            "LUMO_LOCK_HASH (value not shown).")

    raw_2 = (environ.get("LUMO_LOCK_HASH_2") or "").strip()
    salt_2_hex, hash_2_hex = _parse_lumo_lock_hash(
        raw_2, "LUMO_LOCK_HASH_2")
    _write_lumo_lock_header(
        chromium_src, LUMO_LOCK_CONFIG_2_REL_PATH,
        "CHROME_BROWSER_LUMO_LUMO_LOCK_CONFIG_2_H_",
        "kLockSaltHex2", "kLockHashHex2", salt_2_hex, hash_2_hex,
        "LUMO_LOCK_HASH_2")
    if raw_2:
        log("Lumo startup lock: second lock config header written "
            "from LUMO_LOCK_HASH_2 (value not shown).")
        if not raw:
            log("Lumo startup lock: WARNING: LUMO_LOCK_HASH_2 is "
                "set but LUMO_LOCK_HASH is not; the second "
                "password has no effect without the first.")


# Lumo build number (task 6 extension, patch 0025). The product
# version string (156.0.8078.80) is identical for every Lumo build,
# so it cannot key the "show the welcome flow once after an
# upgrade" decision. Instead the CI run number of the build
# workflow is embedded through a generated header, in the same
# style as the startup lock config above: build-windows.yml passes
# github.run_number to build-stage.yml, which exports it as
# LUMO_BUILD_NUMBER for the stage run. The header is rewritten on
# every branding run and is deliberately NOT part of patch 0025.
# A missing or non-numeric value is not an error: the build number
# is 0, which disables the upgrade welcome (patch 0025 treats 0 as
# "no build number embedded"), so local or foreign builds simply
# never show it. Because the header is generated during setup, a
# cross-run resume chain keeps the ORIGINAL run's number (the
# setup phase does not rerun); run numbers grow monotonically, so
# a shipped resume build still sorts after every earlier build.
LUMO_BUILD_CONFIG_REL_PATH = "chrome/browser/lumo/lumo_build_config.h"


def write_lumo_build_config(chromium_src, environ=None):
    """Generate chrome/browser/lumo/lumo_build_config.h from the
    LUMO_BUILD_NUMBER environment variable (the CI run number).

    Mirrors write_lumo_lock_config's hardening: the written file
    is read back and verified byte-for-byte. Unlike the lock hash
    (a secret in a strict format), a malformed build number is
    harmless — it just disables the upgrade welcome — so anything
    that is not a plain non-negative integer in int range becomes
    0 instead of failing the build. The value is not a secret and
    is logged.
    """
    if environ is None:
        environ = os.environ
    raw = (environ.get("LUMO_BUILD_NUMBER") or "").strip()
    number = 0
    if re.fullmatch(r"[0-9]+", raw):
        candidate = int(raw)
        if candidate <= 2147483647:
            number = candidate
    header_path = chromium_src / LUMO_BUILD_CONFIG_REL_PATH
    lines = [
        "// Generated by scripts/ci/stage.py from the LUMO_BUILD_NUMBER",
        "// environment variable (the CI run number of the build) at",
        "// build time. Do not edit; regenerated on every branding run.",
        "// 0 means no build number was embedded (a local or foreign",
        "// build); the upgrade welcome flow is disabled then.",
        "#ifndef CHROME_BROWSER_LUMO_LUMO_BUILD_CONFIG_H_",
        "#define CHROME_BROWSER_LUMO_LUMO_BUILD_CONFIG_H_",
        "",
        "namespace lumo {",
        "",
        f"inline constexpr int kBuildNumber = {number};",
        "",
        "}  // namespace lumo",
        "",
        "#endif  // CHROME_BROWSER_LUMO_LUMO_BUILD_CONFIG_H_",
        "",
    ]
    text = "\n".join(lines)
    header_path.parent.mkdir(parents=True, exist_ok=True)
    header_path.write_text(text, encoding="utf-8")
    written = header_path.read_text(encoding="utf-8")
    if written != text:
        raise StageError(
            "lumo_build_config.h verification failed: the written "
            "file does not match the generated content; refusing "
            "to build with an unverified build configuration"
        )
    if number:
        log(f"Lumo build config: build number {number} embedded "
            "(upgrade welcome enabled).")
    else:
        log("Lumo build config: LUMO_BUILD_NUMBER is not set or "
            "not a plain number; embedding build number 0 "
            "(upgrade welcome disabled).")


def apply_lumo_branding(args, ctx, deadline):
    """Turn the post-setup Thorium tree into Lumo (see header)."""
    repo_root = ctx["repo_root"]
    chromium_src = ctx["chromium_src"]
    env = ctx["env"]
    git = find_executable("git")

    patch_dir = repo_root.joinpath(*("scripts", "ci", "lumo_patches"))
    patches = sorted(patch_dir.glob("*.patch"))
    if not patches:
        raise StageError(f"no Lumo branding patches found in {patch_dir}")
    for patch_path in patches:
        _git_apply_both_ways(git, chromium_src, patch_path, env, deadline)

    asset_dir = repo_root.joinpath(*("scripts", "ci", "lumo_branding"))
    for asset_name, dest_rel in LUMO_ICON_COPIES:
        source = asset_dir / asset_name
        dest = chromium_src / dest_rel
        if not source.is_file():
            raise StageError(f"Lumo branding asset missing: {source}")
        if not dest.parent.is_dir():
            raise StageError(
                f"Lumo icon destination directory missing: {dest.parent}")
        shutil.copyfile(source, dest)
    log(f"Lumo branding: copied the Lumo icon over "
        f"{len(LUMO_ICON_COPIES)} theme resource files.")

    ext_asset_dir = repo_root.joinpath(
        *("scripts", "ci", "lumo_extensions"))
    for asset_name, dest_rel in LUMO_EXTENSION_CRX_COPIES:
        source = ext_asset_dir / asset_name
        dest = chromium_src / dest_rel
        if not source.is_file():
            raise StageError(
                f"Lumo extension asset missing: {source}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
    log(f"Lumo branding: copied {len(LUMO_EXTENSION_CRX_COPIES)} "
        f"preinstalled extension package(s) into the tree.")

    for desc, n in lumo_string_replacements(chromium_src):
        log(f"Lumo branding: {desc}: {n} replacement(s).")
    for desc, n in lumo_systematic_brand_pass(chromium_src):
        log(f"Lumo branding: {desc}: {n} replacement(s).")
    for desc, n in lumo_flag_entries_brand_pass(chromium_src):
        log(f"Lumo branding: {desc}: {n} replacement(s).")
    for desc, n in lumo_flag_translation_pass(chromium_src):
        log(f"Lumo branding: {desc}: {n} replacement(s).")
    for desc, n in lumo_labs_origin_suffix_pass(chromium_src):
        log(f"Lumo branding: {desc}: {n} replacement(s).")
    write_lumo_lock_config(chromium_src)
    write_lumo_build_config(chromium_src)
    log("Lumo branding complete.")


# Marker phase_setup drops in the build root (which the checkpoint
# archives carry from stage to stage) once Thorium's setup.py has
# returned successfully. See _thorium_setup_done().
SETUP_DONE_MARKER_NAME = "lumo-thorium-setup-done.json"

# Thorium setup.py's SIMD profile -> directory under other/ holding
# its thorium_version.txt (setup.py's SIMD_PROFILES; stage.py only
# ever passes avx2 or sse3, enforced by --profile's choices).
SETUP_PROFILE_VERSION_DIRS = {"avx2": "AVX2", "sse3": "SSE3"}


def _record_thorium_setup_done(ctx, args):
    marker = ctx["build_root"] / SETUP_DONE_MARKER_NAME
    payload = {
        "thorium_commit": args.thorium_commit,
        "chromium_tag": args.chromium_tag,
    }
    marker.write_text(json.dumps(payload), encoding="utf-8")


def _thorium_setup_done(ctx, args):
    """Has Thorium's setup.py already completed on this tree?

    Returns a short reason string when it has, else None. Two kinds
    of evidence, strongest first:

    1. The marker _record_thorium_setup_done() writes into the
       build root right after setup.py returns (the checkpoint
       archives carry it into the next stage).
    2. Completion evidence inside the tree itself, covering a stage
       that died after setup.py finished but before the marker
       could be recorded — or a tree set up outside this phase:
       setup.py publishes its ownership manifest before mutating,
       and its LAST mutation for the SIMD profiles is copying
       other/<PROFILE>/thorium_version.txt over
       ui/webui/resources/text/thorium_version.txt, so that
       destination holding exactly the profile source's bytes,
       together with the manifest, means every mutation landed.
    """
    marker = ctx["build_root"] / SETUP_DONE_MARKER_NAME
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        data = None
    if (
        isinstance(data, dict)
        and data.get("thorium_commit") == args.thorium_commit
        and data.get("chromium_tag") == args.chromium_tag
    ):
        return "completion marker recorded by a previous stage"
    chromium_src = ctx["chromium_src"]
    manifest = (
        chromium_src / "out" / "thorium" / ".thorium-setup-files.json"
    )
    version_dir = SETUP_PROFILE_VERSION_DIRS.get(args.profile)
    if version_dir is None or not manifest.is_file():
        return None
    source = (
        ctx["thorium_root"] / "other" / version_dir
        / "thorium_version.txt"
    )
    destination = (
        chromium_src / "ui" / "webui" / "resources" / "text"
        / "thorium_version.txt"
    )
    try:
        if (
            destination.is_file()
            and source.is_file()
            and destination.read_bytes() == source.read_bytes()
        ):
            return "completion evidence in the tree"
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------
# Setup fingerprint (build #30 audit fix)
#
# phase_setup bakes the repo's Lumo inputs into the tree: the patch
# set, stage.py's own generated headers and string passes, the flag
# translation table, and the branding/extension assets it copies.
# A stage that resumes into the gn or build phase never re-runs
# setup, so it compiles whatever its checkpoint's setup produced.
# The resume fixups only cover the spots they were written for;
# anything else a later commit changed would be missing from the
# shipped browser with no error anywhere (a silently feature-
# incomplete build). phase_setup therefore records a fingerprint
# of all its inputs in the build root (which the checkpoint zips
# carry from stage to stage), and the gn/build phases refuse to
# consume a tree whose recorded fingerprint does not match the
# current repo: the fix is a fresh build, not a quieter resume.
# ---------------------------------------------------------------------------

SETUP_FINGERPRINT_FILE_NAME = "lumo-setup-fingerprint.json"


def lumo_setup_fingerprint(repo_root):
    """Hash of everything phase_setup bakes into the tree."""
    digest = hashlib.sha256()

    def feed(label, data):
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")

    ci_dir = repo_root / "scripts" / "ci"
    stage_py = ci_dir / "stage.py"
    if stage_py.is_file():
        feed("stage.py", stage_py.read_bytes())
    translations = ci_dir / "lumo_flag_translations.json"
    if translations.is_file():
        feed("lumo_flag_translations.json", translations.read_bytes())
    for sub in ("lumo_patches", "lumo_branding", "lumo_extensions"):
        folder = ci_dir / sub
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                feed(f"{sub}/{path.relative_to(folder).as_posix()}",
                     path.read_bytes())
    return digest.hexdigest()


def _record_setup_fingerprint(ctx):
    fingerprint = lumo_setup_fingerprint(ctx["repo_root"])
    payload = {"fingerprint": fingerprint}
    marker = ctx["build_root"] / SETUP_FINGERPRINT_FILE_NAME
    marker.write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    log(f"Setup fingerprint recorded: {fingerprint[:16]}...")


def verify_setup_fingerprint(ctx):
    """Refuse a gn/build phase whose tree was set up from other
    inputs than the current repo's (see the section header)."""
    marker = ctx["build_root"] / SETUP_FINGERPRINT_FILE_NAME
    recorded = None
    if marker.is_file():
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            recorded = data.get("fingerprint")
    if not recorded:
        raise StageError(
            "this checkpoint carries no setup fingerprint "
            f"({SETUP_FINGERPRINT_FILE_NAME}): it predates resume "
            "verification, or its setup phase never completed. "
            "Refusing to resume from it; a fresh build (from "
            "stage 1) is required."
        )
    current = lumo_setup_fingerprint(ctx["repo_root"])
    if recorded != current:
        raise StageError(
            "setup fingerprint mismatch: the Lumo patch set, "
            "stage.py, the flag translations, or the branding "
            "assets changed after this tree was set up "
            f"(checkpoint {str(recorded)[:16]}..., current "
            f"{current[:16]}...). Resuming would compile the old "
            "setup's sources and silently ship a browser missing "
            "the newer changes; refusing to resume. A fresh build "
            "(from stage 1) is required."
        )
    log("Setup fingerprint verified: the tree matches the current "
        "Lumo inputs.")


def phase_setup(args, ctx, deadline):
    """Thorium's setup.py: overlays + patch series + GRD rebase.

    Re-running this phase after a budget expiry needs care, because
    Thorium's setup.py is only MOSTLY re-runnable (verified
    empirically against the pinned Thorium, 2026-11-16):

      * The fetch/filter steps below skip files that already exist
        and re-derive everything from the fresh Thorium clone.
      * setup.py copies its overlays with plain overwrites and
        writes its ownership manifest (.thorium-setup-files.json)
        BEFORE mutating the tree, so an interrupted run leaves a
        manifest the next run can clean up from.
      * apply_series.py checks every patch both ways: when the
        forward `git apply --check` fails but the reverse check
        passes, the patch is reported "already applied" and
        SKIPPED, so a series interrupted halfway resumes cleanly.
      * THE EXCEPTION: third_party/ffmpeg's
        other/add-hevc-ffmpeg-decoder-parser.patch, the first of
        four series entries that edit the same generated ffmpeg
        files. Once the later entries (the AC-3 decoders, the HEVC
        nasm fix, ffmpeg branding) have landed on top of it, its
        reverse check no longer matches either, apply_series
        reports it "failed", and setup.py exits 1 — re-running
        setup.py on a tree whose series COMPLETED is fatal.

    This phase therefore never re-runs setup.py on a completed
    tree: _thorium_setup_done() recognizes completion (the marker
    recorded here after setup.py returns, or failing that,
    completion evidence inside the tree) and skips straight to
    the Lumo branding steps, which are idempotent by construction
    (see the branding section header). The one residual window is
    a kill DURING setup.py after the ffmpeg entries landed but
    before its final profile copy: no marker or evidence exists
    yet, and the re-run dies in apply_series as described. That
    window is minutes wide inside a phase that starts with hours
    of budget; if it ever happens, it fails loudly here in the
    setup phase and the run must restart from stage 1.
    """
    done_reason = _thorium_setup_done(ctx, args)
    if done_reason is not None:
        log(
            "\nThorium's setup.py already completed on this tree "
            f"({done_reason}); skipping it and re-running only "
            "the idempotent Lumo branding steps. (Re-running "
            "setup.py on a completed tree is NOT safe: the first "
            "ffmpeg series patch can no longer be recognized as "
            "applied once the later ffmpeg patches land on it.)"
        )
        _record_thorium_setup_done(ctx, args)
        apply_lumo_branding(args, ctx, deadline)
        _record_setup_fingerprint(ctx)
        log("\nSetup phase complete.")
        return
    thorium_root = ctx["thorium_root"]
    chromium_src = ctx["chromium_src"]
    fetch_missing_series_files(
        thorium_root, chromium_src, args.chromium_tag
    )
    fetch_missing_test_fonts(chromium_src, args.chromium_tag)
    filter_unappliable_series(thorium_root, chromium_src)
    apply_lumo_series_overrides(thorium_root)
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
    _record_thorium_setup_done(ctx, args)
    apply_lumo_branding(args, ctx, deadline)
    _record_setup_fingerprint(ctx)
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
        # The value is embedded verbatim in a GN string literal.
        # A quote or backslash in it would be reinterpreted by
        # GN's own string escapes (or break the literal), so the
        # key compiled into the browser would silently differ
        # from the Secret — a successful build with broken Sync
        # and no error anywhere. Real Google API keys and OAuth
        # client secrets never contain these characters; refuse
        # loudly instead of shipping a quietly wrong value.
        if any(c in value for c in '"\\') or any(
            ord(c) < 0x20 or ord(c) == 0x7F for c in value
        ):
            raise StageError(
                f"the secret for {name} contains a character that "
                "cannot be embedded verbatim in a GN string "
                "(quote, backslash, or control character); "
                "refusing to write a mangled args.gn"
            )
        pattern = re.compile(
            rf'^{re.escape(name)}\s*=\s*".*"$', re.MULTILINE
        )
        replacement = f'{name} = "{value}"'
        # A function replacement: re.subn would otherwise treat
        # backslash escapes in the replacement string specially.
        new_text, count = pattern.subn(
            lambda _m: replacement, text
        )
        if count != 1:
            raise StageError(
                f"expected exactly one {name} line in {template}, "
                f"found {count}; template changed?"
            )
        text = new_text
    args_gn = out_dir / "args.gn"
    args_gn.write_text(text, encoding="utf-8")
    # Read the file back and verify every injected line survived
    # byte-for-byte. The values are never printed; a mismatch is
    # reported by key name only.
    written = args_gn.read_text(encoding="utf-8")
    for name, value in keys.items():
        expected = f'{name} = "{value}"'
        if written.count(expected) != 1:
            raise StageError(
                f"args.gn verification failed for {name}: the "
                "written file does not contain the injected line "
                "exactly once; refusing to build with an "
                "unverified API key configuration"
            )
    set_keys = [name for name, value in keys.items() if value]
    if set_keys:
        log(f"args.gn written; API keys injected for: "
            f"{', '.join(set_keys)} (values not shown).")
    else:
        log("args.gn written; no Google API keys set "
            "(Sync will be unavailable, the browser still builds).")
    return args_gn


def restore_generated_secret_files(args, ctx):
    """Re-create the Secret-bearing generated files on resume.

    checkpoint() deliberately keeps args.gn and the startup lock
    config headers out of the checkpoint zip (they carry GitHub
    Secrets into the tree; see _checkpoint_exclusion_switches), so
    a stage resuming from such a checkpoint finds them missing.
    Both generators are pure functions of the stage environment
    (the workflow passes the same Secrets to every stage) and are
    idempotent, so re-running them here restores exactly the bytes
    the original stage produced:

    * args.gn, only when missing (phase_gn rewrites it anyway;
      this covers a resume straight into the build phase, and
      write_args_gn re-verifies the injected lines byte-for-byte);
    * lumo_lock_config.h / lumo_lock_config_2.h, when either is
      missing. Without this the build would still succeed and
      silently ship a browser with the startup lock DISABLED:
      patch 0019 reads the headers through __has_include and
      treats a missing header as "no password configured".
    """
    chromium_src = ctx["chromium_src"]
    out_dir = chromium_src / OUT_DIR_NAME
    if not (out_dir / "args.gn").is_file():
        log("\nResume: args.gn is not in the checkpoint (it is "
            "kept out on purpose; it carries the API keys). "
            "Regenerating it from this stage's environment.")
        write_args_gn(args, ctx)
    lock_header = chromium_src / LUMO_LOCK_CONFIG_REL_PATH
    lock_header_2 = chromium_src / LUMO_LOCK_CONFIG_2_REL_PATH
    if not lock_header.is_file() or not lock_header_2.is_file():
        log("\nResume: the startup lock config header(s) are not "
            "in the checkpoint (kept out on purpose; they carry "
            "the lock hash). Regenerating them from this stage's "
            "environment.")
        write_lumo_lock_config(chromium_src)


def phase_gn(args, ctx, deadline):
    """Generate the Ninja build files with `gn gen`."""
    # A resumed tree must have been set up from the current repo's
    # inputs, and its Secret-bearing generated files (kept out of
    # the checkpoint zip) must be restored before anything reads
    # them. Both are no-ops on a from-scratch run of this stage.
    verify_setup_fingerprint(ctx)
    restore_generated_secret_files(args, ctx)
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


# ---------------------------------------------------------------------------
# Resume fixups (build #19 -> the resume run)
#
# A stage that resumes from a checkpoint recorded in the "build"
# phase never re-runs phase_setup, so a checkpointed tree keeps the
# sources its own setup phase produced — the Lumo patches that were
# current when the tree was SET UP, not when it is resumed. Run
# #19's tree was set up with the pre-fix patches 0008/0009 and died
# in build-4 on the Chromium 156 API mismatches they contained
# (BookmarkModelFactory::GetForProfile, the missing SyncCycleSnapshot
# include, the InMillisecondsFSinceUnixEpochF typo, and
# views::Label::set_focusable). The patches are fixed, but a resume
# from #19's state-3 checkpoint would compile the OLD sources and
# fail the same way, so apply_lumo_resume_fixups() rewrites exactly
# those four spots in a resumed tree, byte for byte the way the
# fixed patches leave them (verified byte-identical against a fresh
# tree set up with the fixed patches). On a from-scratch build the
# setup phase applies the fixed patches, so every fixup finds its
# target already fixed and is a no-op.
#
# The same shape covers build #24's grit failure: patch 0029 adds
# the lumo_tools first-id entry to tools/gritsettings/resource_ids.spec,
# but a resume from #24's state-2 checkpoint keeps the setup-time
# .spec without it, so _resume_fixup_grit_spec_entry() inserts the
# entry after the location_internals anchor instead. The gen copy
# grit reads is regenerated from the .spec by ninja, so the .spec
# rewrite is the whole fix on the resume path too.
#
# The same shape covers build #25's lint failure: patch 0022 adds
# the Lumo Monitor page but forgot the settings_main.ts import
# the webui-eslint web-component-missing-deps rule requires, so
# _resume_fixup_lumo_monitor_import() inserts it after the
# autofill index import in a resumed tree.
#
# The same shape covers build #26's compile failure: patch 0016
# (tracking protection strict mode) used CookieControlsMode
# unqualified in profile_impl.cc, but in Chromium 156 the enum
# lives in namespace content_settings (defined in
# components/content_settings/core/browser/cookie_settings.h,
# which profile_impl.cc already includes), so build-3 died with
# "use of undeclared identifier 'CookieControlsMode'". The fixed
# patch 0016 qualifies it as content_settings::CookieControlsMode,
# and the plain replacement below carries the same bytes into a
# resumed tree. The replacement anchors on the full
# "static_cast<int>(CookieControlsMode..." form so the fixed
# bytes (which contain the enum name as a substring) can never
# be mistaken for the pre-fix form.
#
# The same shape covers build #27's compile failure: patches 0017
# (full-page screenshot) and 0018 (tab-group session save) call
# base::JSONReader::Read with one argument, but in Chromium 156
# the options parameter has no default (Read(std::string_view,
# int, size_t)), so build-4 died with "too few arguments to
# function 'base::JSONReader::Read'". The fixed patches pass
# base::JSON_PARSE_RFC (0, strict RFC parsing, the old default
# behaviour). The same build also proved patch 0022 (Lumo Monitor)
# uncompilable: Chromium 156's base/values.h has no nested
# base::Value::List / base::Value::Dict types any more — the
# containers are top-level base::ListValue / base::DictValue — and
# chrome::DIR_USER_DATA_DIR does not exist in chrome_paths.h
# (the key is chrome::DIR_USER_DATA), so the handler died with
# "no type named 'List'/'Dict' in base::Value" and "no member
# named 'DIR_...' in chrome". Patch 0020 (debugger reporter) uses
# the same three spellings and had not been reached yet (build-4
# stopped at 45.2% with 10173 steps remaining), so it is fixed
# the same way before it can fail. The plain replacements below
# carry the same bytes into a resumed tree; each anchors on the
# full pre-fix spelling, so the fixed bytes can never be mistaken
# for the pre-fix form.
#
# The same shape covers build #28's compile failure: patch 0020's
# lumo_debugger_reporter.cc passes a fire-and-forget lambda to
# network::SimpleURLLoader::DownloadToString whose body
# parameter is std::unique_ptr<std::string>, but in Chromium 156
# BodyAsStringCallback is OnceCallback<void(std::optional<
# std::string>)> — body delivery switched from a nullable
# unique_ptr to std::optional — so build-5 died with "no viable
# conversion from 'OnceCallback<UnboundRunType>' to
# 'OnceCallback<void (std::optional<std::string>)>'". The fixed
# patch takes std::optional<std::string> (the body itself is
# ignored; the callback only destroys the self-owned loader)
# and includes <optional> explicitly. The two plain replacements
# below carry the same bytes into a resumed tree: the include
# replacement anchors on the adjacent <memory>/<set> include
# pair, which the fixed form no longer contains contiguously,
# and the lambda replacement anchors on the full two-line
# signature, so neither can be mistaken for the fixed form.
#
# The same shape covers build #30's settings-menu fix: patch
# 0022 pointed the Lumo Monitor menu item at the cr-icon
# "settings:dashboard", but the dashboard glyph only exists in
# the settings20 iconset (chrome/browser/resources/settings/
# icons.html.ts defines it in both settings20 variants and in
# neither settings variant), so the item rendered with a blank
# icon in build #29's real-machine check — every other menu item
# resolves, including the one settings20 icon already in use
# ("settings20:button-magic"). The fixed patch 0022 uses
# "settings20:dashboard", and the plain replacement below
# carries the same bytes into a resumed tree.
#
# The same shape covers build #30's real-machine fixes from
# build #29's check, all in the plain replacements below:
# patch 0010's welcome screens get a flex gap (the padded
# product logo, heading and button touched) and a visually
# distinct credits screen (its own class in the refresh HTML);
# the animation logo file itself is rewritten to the padded
# variant stage.py now copies (the FRE stylesheet crops the
# central half of the animation svg, which is 50% padding by
# upstream design; the unpadded Lumo mark overflowed the logo
# box onto the heading); patch 0030 makes Thorium's display
# branding rewrite internal URLs to lumo:// instead of
# thorium:// (the input-side alias of patch 0023 already
# accepted lumo://, but displays kept saying thorium://);
# patch 0022's Monitor page renames the owner's-API-key label
# to "Google API key" with embedded/not-embedded values, and
# its signed-in check moves from ConsentLevel::kSync to
# kSignin (under the account-storage model a signed-in
# profile holds its account at kSignin, so the Monitor
# reported "not signed in" for a signed-in profile); patch
# 0019's startup lock learns the optional second password
# (either password unlocks; the unlock token is derived from
# whichever entry matched). The lock's second config header
# needs no fixup: it is generated during setup, and a resumed
# tree without it simply builds with one password, exactly
# like a tree set up before the second password existed.
#
# Patch 0025's picker-hosted upgrade welcome (build #30) is
# deliberately NOT carried by a fixup: it rewires six files
# (profile picker params/view, the first-run flow controller,
# the startup browser creator) and removes the startup-tab
# insertion, which is far beyond a byte spot-fix. A build
# resuming into the build phase must therefore come from a
# checkpoint whose setup already ran with the new patch 0025
# (a fresh #30 build, or a later #30 checkpoint) — resuming a
# pre-#30 checkpoint into #30's build phase would compile the
# old tab-based welcome, not fail, so this is a silent
# behaviour difference to be aware of, not an error.
# ---------------------------------------------------------------------------

_PEOPLE_HANDLER_REL = "chrome/browser/ui/webui/settings/people_handler.cc"
_BROWSER_VIEW_REL = "chrome/browser/ui/views/frame/browser_view.cc"
_PROFILE_IMPL_REL = "chrome/browser/profiles/profile_impl.cc"
_BROWSER_COMMAND_CONTROLLER_REL = (
    "chrome/browser/ui/browser_command_controller.cc"
)
_LUMO_MONITOR_HANDLER_REL = (
    "chrome/browser/ui/webui/settings/lumo_monitor_handler.cc"
)
_LUMO_DEBUGGER_REPORTER_REL = "chrome/browser/lumo/lumo_debugger_reporter.cc"
_SETTINGS_MENU_REL = (
    "chrome/browser/resources/settings/settings_menu/settings_menu.html"
)
_SETTINGS_STRINGS_REL = "chrome/app/settings_strings.grdp"
_LUMO_MONITOR_PAGE_REL = (
    "chrome/browser/resources/settings/lumo_monitor_page/"
    "lumo_monitor_page.ts"
)
_SIGN_IN_PROMO_REFRESH_CSS_REL = (
    "chrome/browser/resources/intro/sign_in_promo_refresh.css"
)
_SIGN_IN_PROMO_REFRESH_HTML_REL = (
    "chrome/browser/resources/intro/sign_in_promo_refresh.html.ts"
)
_INTRO_APP_CSS_REL = "chrome/browser/resources/intro/intro_app.css"
_PRODUCT_LOGO_ANIMATION_REL = (
    "chrome/app/theme/chromium/product_logo_animation.svg"
)
_THORIUM_URL_UTILS_REL = "chrome/browser/thorium_url_utils.cc"
_THORIUM_EASTER_EGGS_REL = (
    "chrome/browser/ui/webui/thorium_easter_eggs.h"
)
_CHROME_BROWSER_MAIN_WIN_REL = "chrome/browser/chrome_browser_main_win.cc"

# (file, old bytes, new bytes, what) — plain byte replacements.
_LUMO_RESUME_FIXUP_REPLACEMENTS = (
    (
        _PEOPLE_HANDLER_REL,
        b"BookmarkModelFactory::GetForProfile(profile_)",
        b"BookmarkModelFactory::GetForBrowserContext(profile_)",
        "BookmarkModelFactory::GetForProfile -> GetForBrowserContext",
    ),
    (
        _PEOPLE_HANDLER_REL,
        b"InMillisecondsFSinceUnixEpochF()",
        b"InMillisecondsFSinceUnixEpoch()",
        "InMillisecondsFSinceUnixEpochF -> InMillisecondsFSinceUnixEpoch",
    ),
    (
        _BROWSER_VIEW_REL,
        b"lumo_dev_stage_label->set_focusable(false);",
        b"lumo_dev_stage_label->SetFocusBehavior("
        b"views::View::FocusBehavior::NEVER);",
        "views::Label set_focusable -> SetFocusBehavior(NEVER)",
    ),
    (
        _PROFILE_IMPL_REL,
        b"static_cast<int>(CookieControlsMode::kBlockThirdParty)",
        b"static_cast<int>(content_settings::CookieControlsMode::"
        b"kBlockThirdParty)",
        "CookieControlsMode -> content_settings::CookieControlsMode",
    ),
    (
        _BROWSER_COMMAND_CONTROLLER_REL,
        b"    std::optional<base::Value> parsed = base::JSONReader::Read(\n"
        b"        std::string_view(reinterpret_cast<const char*>(message.data()),\n"
        b"                         message.size()));",
        b"    std::optional<base::Value> parsed = base::JSONReader::Read(\n"
        b"        std::string_view(reinterpret_cast<const char*>(message.data()),\n"
        b"                         message.size()),\n"
        b"        base::JSON_PARSE_RFC);",
        "JSONReader::Read (screenshot) -> JSONReader::Read(..., JSON_PARSE_RFC)",
    ),
    (
        _BROWSER_COMMAND_CONTROLLER_REL,
        b"base::JSONReader::Read(*json);",
        b"base::JSONReader::Read(*json, base::JSON_PARSE_RFC);",
        "JSONReader::Read (tab groups) -> JSONReader::Read(..., JSON_PARSE_RFC)",
    ),
    (
        _LUMO_MONITOR_HANDLER_REL,
        b"base::Value::List",
        b"base::ListValue",
        "base::Value::List -> base::ListValue (Lumo Monitor)",
    ),
    (
        _LUMO_MONITOR_HANDLER_REL,
        b"base::Value::Dict",
        b"base::DictValue",
        "base::Value::Dict -> base::DictValue (Lumo Monitor)",
    ),
    (
        _LUMO_MONITOR_HANDLER_REL,
        b"chrome::DIR_USER_DATA_DIR",
        b"chrome::DIR_USER_DATA",
        "chrome::DIR_USER_DATA_DIR -> chrome::DIR_USER_DATA (Lumo Monitor)",
    ),
    (
        _LUMO_DEBUGGER_REPORTER_REL,
        b"base::Value::List",
        b"base::ListValue",
        "base::Value::List -> base::ListValue (debugger reporter)",
    ),
    (
        _LUMO_DEBUGGER_REPORTER_REL,
        b"base::Value::Dict",
        b"base::DictValue",
        "base::Value::Dict -> base::DictValue (debugger reporter)",
    ),
    (
        _LUMO_DEBUGGER_REPORTER_REL,
        b"chrome::DIR_USER_DATA_DIR",
        b"chrome::DIR_USER_DATA",
        "chrome::DIR_USER_DATA_DIR -> chrome::DIR_USER_DATA (debugger reporter)",
    ),
    (
        _LUMO_DEBUGGER_REPORTER_REL,
        b"#include <memory>\n#include <set>\n",
        b"#include <memory>\n#include <optional>\n#include <set>\n",
        "#include <optional> (debugger reporter)",
    ),
    (
        _LUMO_DEBUGGER_REPORTER_REL,
        b"          [](std::unique_ptr<network::SimpleURLLoader> owned_loader,\n"
        b"             std::unique_ptr<std::string> body) {",
        b"          [](std::unique_ptr<network::SimpleURLLoader> owned_loader,\n"
        b"             std::optional<std::string> body) {",
        "DownloadToString callback body std::unique_ptr<std::string> -> "
        "std::optional<std::string> (debugger reporter)",
    ),
    (
        _LUMO_DEBUGGER_REPORTER_REL,
        b"bool IsEnabled() {\n"
        b"  const base::CommandLine* command_line =\n"
        b"      base::CommandLine::ForCurrentProcess();\n"
        b"  return command_line->HasSwitch(kEndpointSwitch) &&\n"
        b"         !command_line->GetSwitchValueASCII(kEndpointSwitch).empty();\n"
        b"}\n"
        b"\n"
        b"namespace {",
        b"namespace {\n"
        b"\n"
        b"// The port the Lumo debugger app listens on, and the only port a\n"
        b"// valid endpoint may name (the app binds 127.0.0.1:47741).\n"
        b"constexpr int kDebuggerPort = 47741;\n"
        b"\n"
        b"// Parses the --lumo-debugger-endpoint value and returns it only\n"
        b"// when it points at the debugger app on this machine: the http\n"
        b"// scheme, a loopback host (127.0.0.1, localhost, or ::1), and\n"
        b"// kDebuggerPort. Anything else (a remote host, another port,\n"
        b"// https, a malformed URL) yields nullopt, so a stray or hostile\n"
        b"// switch value can never make the browser POST session events\n"
        b"// anywhere but the owner's own debugger app.\n"
        b"std::optional<GURL> GetValidatedEndpoint() {\n"
        b"  const base::CommandLine* command_line =\n"
        b"      base::CommandLine::ForCurrentProcess();\n"
        b"  if (!command_line->HasSwitch(kEndpointSwitch)) {\n"
        b"    return std::nullopt;\n"
        b"  }\n"
        b"  const GURL url(command_line->GetSwitchValueASCII(kEndpointSwitch));\n"
        b"  if (!url.is_valid() || !url.SchemeIs(\"http\")) {\n"
        b"    return std::nullopt;\n"
        b"  }\n"
        b"  const std::string host(url.host());\n"
        b"  if (host != \"127.0.0.1\" && host != \"localhost\" && host != \"::1\") {\n"
        b"    return std::nullopt;\n"
        b"  }\n"
        b"  if (url.EffectiveIntPort() != kDebuggerPort) {\n"
        b"    return std::nullopt;\n"
        b"  }\n"
        b"  return url;\n"
        b"}\n"
        b"\n"
        b"}  // namespace\n"
        b"\n"
        b"bool IsEnabled() {\n"
        b"  return GetValidatedEndpoint().has_value();\n"
        b"}\n"
        b"\n"
        b"namespace {",
        "endpoint validation: GetValidatedEndpoint + IsEnabled via it "
        "(debugger reporter, patch 0020)",
    ),
    (
        _LUMO_DEBUGGER_REPORTER_REL,
        b"  request->url = GURL(base::CommandLine::ForCurrentProcess()\n"
        b"                          ->GetSwitchValueASCII(kEndpointSwitch));\n"
        b"  if (!request->url.is_valid()) {\n"
        b"    return;\n"
        b"  }\n",
        b"  std::optional<GURL> endpoint = GetValidatedEndpoint();\n"
        b"  if (!endpoint) {\n"
        b"    return;\n"
        b"  }\n"
        b"  request->url = *endpoint;\n",
        "PostJson posts to the validated endpoint only "
        "(debugger reporter, patch 0020)",
    ),
    (
        _SETTINGS_MENU_REL,
        b'<cr-icon icon="settings:dashboard"></cr-icon>',
        b'<cr-icon icon="settings20:dashboard"></cr-icon>',
        "settings menu icon settings:dashboard -> settings20:dashboard "
        "(Lumo Monitor)",
    ),
    (
        _SETTINGS_STRINGS_REL,
        b"    <message name=\"IDS_SETTINGS_LUMO_MONITOR_API_KEY_LABEL\" desc=\"Label for the owner's API key presence on the Lumo Monitor settings page (Lumo patch 0022)\">\n"
        b"      \xe8\x87\xaa\xe5\x89\x8d API \xe3\x82\xad\xe3\x83\xbc\n",
        b"    <message name=\"IDS_SETTINGS_LUMO_MONITOR_API_KEY_LABEL\" desc=\"Label for the embedded Google API key on the Lumo Monitor settings page (Lumo patch 0022)\">\n"
        b"      Google API \xe3\x82\xad\xe3\x83\xbc\n",
        "Monitor API key label: owner's API key -> Google API key",
    ),
    (
        _LUMO_MONITOR_PAGE_REL,
        b"    if (this.info_.apiKeyConfigured && this.info_.oauthClientConfigured) {\n"
        b"      return 'API \xe3\x82\xad\xe3\x83\xbc\xe3\x81\xa8 OAuth \xe3\x82\xaf\xe3\x83\xa9\xe3\x82\xa4\xe3\x82\xa2\xe3\x83\xb3\xe3\x83\x88\xe3\x81\x8c\xe7\xb5\x84\xe3\x81\xbf\xe8\xbe\xbc\xe3\x81\xbe\xe3\x82\x8c\xe3\x81\xa6\xe3\x81\x84\xe3\x81\xbe\xe3\x81\x99';\n"
        b"    }\n"
        b"    if (!this.info_.apiKeyConfigured) {\n"
        b"      return 'API \xe3\x82\xad\xe3\x83\xbc\xe3\x81\x8c\xe7\xb5\x84\xe3\x81\xbf\xe8\xbe\xbc\xe3\x81\xbe\xe3\x82\x8c\xe3\x81\xa6\xe3\x81\x84\xe3\x81\xbe\xe3\x81\x9b\xe3\x82\x93';\n"
        b"    }\n"
        b"    return 'OAuth \xe3\x82\xaf\xe3\x83\xa9\xe3\x82\xa4\xe3\x82\xa2\xe3\x83\xb3\xe3\x83\x88\xe3\x81\x8c\xe7\xb5\x84\xe3\x81\xbf\xe8\xbe\xbc\xe3\x81\xbe\xe3\x82\x8c\xe3\x81\xa6\xe3\x81\x84\xe3\x81\xbe\xe3\x81\x9b\xe3\x82\x93';\n",
        b"    if (this.info_.apiKeyConfigured && this.info_.oauthClientConfigured) {\n"
        b"      return '\xe7\xb5\x84\xe3\x81\xbf\xe8\xbe\xbc\xe3\x81\xbf\xe6\xb8\x88\xe3\x81\xbf\xef\xbc\x88\xe3\x82\xb5\xe3\x82\xa4\xe3\x83\xb3\xe3\x82\xa4\xe3\x83\xb3\xe3\x81\xa8\xe5\x90\x8c\xe6\x9c\x9f\xe3\x81\x8c\xe4\xbd\xbf\xe3\x81\x88\xe3\x81\xbe\xe3\x81\x99\xef\xbc\x89';\n"
        b"    }\n"
        b"    return '\xe6\x9c\xaa\xe7\xb5\x84\xe3\x81\xbf\xe8\xbe\xbc\xe3\x81\xbf';\n",
        "Monitor API key text -> embedded / not embedded",
    ),
    (
        _LUMO_MONITOR_HANDLER_REL,
        b"  info.Set(\"syncSignedIn\",\n"
        b"           identity_manager &&\n"
        b"               identity_manager->HasPrimaryAccount(\n"
        b"                   signin::ConsentLevel::kSync));",
        b"  // Signed-in means a primary account at the sign-in consent\n"
        b"  // level: under the account-storage model the account is held\n"
        b"  // at kSignin, and requiring kSync here reported a signed-in\n"
        b"  // profile as \"not signed in\" (build #29 real-machine check).\n"
        b"  info.Set(\"syncSignedIn\",\n"
        b"           identity_manager &&\n"
        b"               identity_manager->HasPrimaryAccount(\n"
        b"                   signin::ConsentLevel::kSignin));",
        "Monitor syncSignedIn: ConsentLevel::kSync -> kSignin",
    ),
    (
        _SIGN_IN_PROMO_REFRESH_CSS_REL,
        b"/* Lumo: welcome-flow screens (Lumo patch 0010). */\n"
        b".lumo-welcome {\n"
        b"  align-items: center;\n"
        b"  display: flex;\n"
        b"  flex-direction: column;\n"
        b"  text-align: center;\n"
        b"}\n"
        b"\n"
        b".lumo-subtitle {\n"
        b"  color: var(--cr-secondary-text-color);\n"
        b"  font-size: 14px;\n"
        b"  line-height: 20px;\n"
        b"  max-width: 520px;\n"
        b"}\n"
        b"\n"
        b".lumo-credits {\n"
        b"  color: var(--cr-secondary-text-color);\n"
        b"  font-size: 13px;\n"
        b"  line-height: 20px;\n"
        b"  max-width: 520px;\n"
        b"}\n",
        b"/* Lumo: welcome-flow screens (Lumo patch 0010). The gap keeps\n"
        b"   the (padded) product logo, the heading and the button from\n"
        b"   touching; the credits screen gets wider spacing and an\n"
        b"   underlined heading so it does not read as a second copy of\n"
        b"   the welcome screen when the flow passes through it. */\n"
        b".lumo-welcome {\n"
        b"  align-items: center;\n"
        b"  display: flex;\n"
        b"  flex-direction: column;\n"
        b"  gap: 1.5rem;\n"
        b"  text-align: center;\n"
        b"}\n"
        b"\n"
        b".lumo-subtitle {\n"
        b"  color: var(--cr-secondary-text-color);\n"
        b"  font-size: 14px;\n"
        b"  line-height: 20px;\n"
        b"  max-width: 520px;\n"
        b"}\n"
        b"\n"
        b".lumo-credits {\n"
        b"  color: var(--cr-secondary-text-color);\n"
        b"  font-size: 13px;\n"
        b"  line-height: 20px;\n"
        b"  max-width: 520px;\n"
        b"}\n"
        b"\n"
        b".lumo-credits p {\n"
        b"  margin: 8px 0;\n"
        b"}\n"
        b"\n"
        b".lumo-credits-screen {\n"
        b"  gap: 2rem;\n"
        b"}\n"
        b"\n"
        b".lumo-credits-screen .title {\n"
        b"  border-bottom: 2px solid var(--cr-fallback-color-primary);\n"
        b"  padding-bottom: 12px;\n"
        b"}\n",
        "welcome/credits screens: gap + credits differentiation "
        "(intro refresh CSS)",
    ),
    (
        _SIGN_IN_PROMO_REFRESH_HTML_REL,
        b"<div class=\"lumo-welcome\">\n"
        b"  <h1 class=\"title fade-in\">$i18n{lumoCreditsHeading}</h1>",
        b"<div class=\"lumo-welcome lumo-credits-screen\">\n"
        b"  <h1 class=\"title fade-in\">$i18n{lumoCreditsHeading}</h1>",
        "credits screen class (intro refresh HTML)",
    ),
    (
        _INTRO_APP_CSS_REL,
        b".lumo-view {\n"
        b"  align-items: center;\n"
        b"  display: flex;\n"
        b"  flex-direction: column;\n"
        b"  height: 100%;",
        b".lumo-view {\n"
        b"  align-items: center;\n"
        b"  display: flex;\n"
        b"  flex-direction: column;\n"
        b"  gap: 1.5rem;\n"
        b"  height: 100%;",
        "welcome screen gap (intro app CSS)",
    ),
    (
        _PRODUCT_LOGO_ANIMATION_REL,
        b"<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 256 256\"><rect x=\"4\" y=\"4\" width=\"248\" height=\"248\" rx=\"52\" fill=\"#191d49\"/><path d=\"M72 48h38v124h78v36H72z\" fill=\"#00a8de\"/></svg>\n",
        b"<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 512 512\"><g transform=\"translate(128 128)\"><rect x=\"4\" y=\"4\" width=\"248\" height=\"248\" rx=\"52\" fill=\"#191d49\"/><path d=\"M72 48h38v124h78v36H72z\" fill=\"#00a8de\"/></g></svg>\n",
        "product_logo_animation.svg -> padded (50% padding) variant",
    ),
    (
        _THORIUM_URL_UTILS_REL,
        b"replacements.SetSchemeStr(content::kThoriumUIScheme);",
        b"replacements.SetSchemeStr(content::kLumoUIScheme);",
        "display URL scheme: thorium -> lumo (GetInternalURLForDisplay)",
    ),
    (
        _THORIUM_URL_UTILS_REL,
        b"  constexpr std::u16string_view kThoriumScheme = u\"thorium:\";\n"
        b"  if (!url.SchemeIs(content::kChromeUIScheme) &&\n"
        b"      !url.SchemeIs(content::kThoriumUIScheme)) {",
        b"  constexpr std::u16string_view kThoriumScheme = u\"thorium:\";\n"
        b"  constexpr std::u16string_view kLumoScheme = u\"lumo:\";\n"
        b"  if (!url.SchemeIs(content::kChromeUIScheme) &&\n"
        b"      !url.SchemeIs(content::kThoriumUIScheme) &&\n"
        b"      !url.SchemeIs(content::kLumoUIScheme)) {",
        "display URL text: accept lumo scheme (GetInternalURLTextForDisplay)",
    ),
    (
        _THORIUM_URL_UTILS_REL,
        b"    source_scheme = kThoriumScheme;\n"
        b"  } else {\n"
        b"    return std::u16string(formatted_url);",
        b"    source_scheme = kThoriumScheme;\n"
        b"  } else if (base::StartsWith(formatted_url, kLumoScheme,\n"
        b"                              base::CompareCase::INSENSITIVE_ASCII)) {\n"
        b"    source_scheme = kLumoScheme;\n"
        b"  } else {\n"
        b"    return std::u16string(formatted_url);",
        "display URL text: lumo prefix (GetInternalURLTextForDisplay)",
    ),
    (
        _THORIUM_URL_UTILS_REL,
        b"IsInternalURLSchemeBrandingEnabled() ? u\"thorium\" : u\"chrome\";",
        b"IsInternalURLSchemeBrandingEnabled() ? u\"lumo\" : u\"chrome\";",
        "display URL text scheme: thorium -> lumo",
    ),
    (
        _THORIUM_EASTER_EGGS_REL,
        b"<title>Thorium Easter Eggs</title>",
        b"<title>Lumo Easter Eggs</title>",
        "eggs page title: Thorium -> Lumo (patch 0040)",
    ),
    (
        _THORIUM_EASTER_EGGS_REL,
        b"Thorium Easter Eggs WebUI Page",
        b"Lumo Easter Eggs WebUI Page",
        "eggs page heading: Thorium -> Lumo (patch 0040)",
    ),
    (
        _THORIUM_EASTER_EGGS_REL,
        b"<title>Thorium Robot</title>",
        b"<title>Lumo Robot</title>",
        "robot page title: Thorium -> Lumo (patch 0040)",
    ),
    (
        _THORIUM_EASTER_EGGS_REL,
        b'alt="Thorium Robot"',
        b'alt="Lumo Robot"',
        "robot image alt: Thorium -> Lumo (patch 0040)",
    ),
    (
        _THORIUM_EASTER_EGGS_REL,
        b"<title>Thorium Authors</title>",
        b"<title>Lumo Authors</title>",
        "authors page title: Thorium -> Lumo (patch 0040)",
    ),
    (
        _THORIUM_EASTER_EGGS_REL,
        b"<h1>Thorium Authors</h1>",
        b"<h1>Lumo Authors</h1>",
        "authors page heading: Thorium -> Lumo (patch 0040)",
    ),

    (
        _CHROME_BROWSER_MAIN_WIN_REL,
        b"#if __has_include(\"chrome/browser/lumo/lumo_lock_config.h\")\n"
        b"#include \"chrome/browser/lumo/lumo_lock_config.h\"\n"
        b"#define LUMO_STARTUP_LOCK_CONFIGURED 1\n"
        b"#endif  // __has_include(\"chrome/browser/lumo/lumo_lock_config.h\")\n"
        b"\n"
        b"namespace {\n",
        b"#if __has_include(\"chrome/browser/lumo/lumo_lock_config.h\")\n"
        b"#include \"chrome/browser/lumo/lumo_lock_config.h\"\n"
        b"#define LUMO_STARTUP_LOCK_CONFIGURED 1\n"
        b"#endif  // __has_include(\"chrome/browser/lumo/lumo_lock_config.h\")\n"
        b"\n"
        b"// Lumo (patch 0019): the optional second password (build #30)\n"
        b"// lives in its own generated header, so a tree set up before it\n"
        b"// existed simply builds without a second password.\n"
        b"#if __has_include(\"chrome/browser/lumo/lumo_lock_config_2.h\")\n"
        b"#include \"chrome/browser/lumo/lumo_lock_config_2.h\"\n"
        b"#define LUMO_STARTUP_LOCK_CONFIGURED_2 1\n"
        b"#endif  // __has_include(\"chrome/browser/lumo/lumo_lock_config_2.h\")\n"
        b"\n"
        b"namespace {\n",
        "lumo_lock_config_2.h include (startup lock)",
    ),
    (
        _CHROME_BROWSER_MAIN_WIN_REL,
        b"#if defined(LUMO_STARTUP_LOCK_CONFIGURED)\n"
        b"constexpr std::string_view kLumoLockSaltHex = lumo::kLockSaltHex;\n"
        b"constexpr std::string_view kLumoLockHashHex = lumo::kLockHashHex;\n"
        b"#else\n"
        b"constexpr std::string_view kLumoLockSaltHex;\n"
        b"constexpr std::string_view kLumoLockHashHex;\n"
        b"#endif\n"
        b"\n"
        b"constexpr wchar_t kLumoUnlockTokenFileName[] = "
        b"L\"lumo-unlock-token\";\n",
        b"#if defined(LUMO_STARTUP_LOCK_CONFIGURED)\n"
        b"constexpr std::string_view kLumoLockSaltHex = lumo::kLockSaltHex;\n"
        b"constexpr std::string_view kLumoLockHashHex = lumo::kLockHashHex;\n"
        b"#else\n"
        b"constexpr std::string_view kLumoLockSaltHex;\n"
        b"constexpr std::string_view kLumoLockHashHex;\n"
        b"#endif\n"
        b"\n"
        b"#if defined(LUMO_STARTUP_LOCK_CONFIGURED_2)\n"
        b"constexpr std::string_view kLumoLockSaltHex2 = lumo::kLockSaltHex2;\n"
        b"constexpr std::string_view kLumoLockHashHex2 = lumo::kLockHashHex2;\n"
        b"#else\n"
        b"constexpr std::string_view kLumoLockSaltHex2;\n"
        b"constexpr std::string_view kLumoLockHashHex2;\n"
        b"#endif\n"
        b"\n"
        b"constexpr wchar_t kLumoUnlockTokenFileName[] = "
        b"L\"lumo-unlock-token\";\n",
        "second lock password constants (startup lock)",
    ),
    (
        _CHROME_BROWSER_MAIN_WIN_REL,
        b"// compiled into the binary; the password itself never is.\n",
        b"// compiled into the binary; the password itself never is. A\n"
        b"// second, optional password (the LUMO_LOCK_HASH_2 secret) is\n"
        b"// configured the same way in lumo_lock_config_2.h; either\n"
        b"// password unlocks.\n",
        "second lock password comment (startup lock)",
    ),
    (
        _CHROME_BROWSER_MAIN_WIN_REL,
        b"struct LumoLockDialogState {\n"
        b"  std::vector<uint8_t> salt;\n"
        b"  std::vector<uint8_t> expected_hash;\n"
        b"  int attempts_left = kLumoLockMaxAttempts;\n"
        b"  bool unlocked = false;\n"
        b"};\n",
        b"struct LumoLockDialogState {\n"
        b"  std::vector<LumoLockEntry> entries;\n"
        b"  int matched_entry = -1;\n"
        b"  int attempts_left = kLumoLockMaxAttempts;\n"
        b"  bool unlocked = false;\n"
        b"};\n",
        "LumoLockDialogState entries (startup lock)",
    ),
    (
        _CHROME_BROWSER_MAIN_WIN_REL,
        b"          if (LumoPasswordMatches(base::WideToUTF8(password), state->salt,\n"
        b"                                  state->expected_hash)) {\n"
        b"            state->unlocked = true;\n"
        b"            EndDialog(dialog, IDOK);\n"
        b"          } else if (--state->attempts_left <= 0) {\n",
        b"          const std::string password_utf8 =\n"
        b"              base::WideToUTF8(password);\n"
        b"          int matched_entry = -1;\n"
        b"          for (size_t i = 0; i < state->entries.size(); ++i) {\n"
        b"            if (LumoPasswordMatches(password_utf8,\n"
        b"                                    state->entries[i].salt,\n"
        b"                                    state->entries[i].expected_hash)) {\n"
        b"              matched_entry = static_cast<int>(i);\n"
        b"              break;\n"
        b"            }\n"
        b"          }\n"
        b"          if (matched_entry >= 0) {\n"
        b"            state->matched_entry = matched_entry;\n"
        b"            state->unlocked = true;\n"
        b"            EndDialog(dialog, IDOK);\n"
        b"          } else if (--state->attempts_left <= 0) {\n",
        "lock dialog tries every entry (startup lock)",
    ),
    (
        _CHROME_BROWSER_MAIN_WIN_REL,
        b"bool RunStartupPasswordLock(const base::FilePath& user_data_dir) {\n"
        b"  if (kLumoLockSaltHex.empty() || kLumoLockHashHex.empty()) {\n"
        b"    return true;  // The lock is not configured for this build.\n"
        b"  }\n"
        b"  std::vector<uint8_t> salt;\n"
        b"  std::vector<uint8_t> expected_hash;\n"
        b"  if (!base::HexStringToBytes(kLumoLockSaltHex, &salt) ||\n"
        b"      !base::HexStringToBytes(kLumoLockHashHex, &expected_hash) ||\n"
        b"      salt.size() != 16 || expected_hash.size() != crypto::kSHA256Length) {\n"
        b"    // stage.py validates the injected value before writing the\n"
        b"    // header, so this can only be a hand-corrupted header. Do not\n"
        b"    // brick the browser over it; the lock stays off and this is\n"
        b"    // logged.\n"
        b"    LOG(ERROR) << \"Lumo startup lock: malformed injected hash; \"\n"
        b"                  \"lock disabled.\";\n"
        b"    return true;\n"
        b"  }\n"
        b"\n"
        b"  const base::FilePath token_path =\n"
        b"      user_data_dir.Append(kLumoUnlockTokenFileName);\n"
        b"  std::string stored_token;\n"
        b"  if (base::ReadFileToString(token_path, &stored_token) &&\n"
        b"      stored_token == LumoUnlockToken(salt, expected_hash)) {\n"
        b"    return true;  // Already unlocked on a previous launch.\n"
        b"  }\n"
        b"\n"
        b"  LumoLockDialogState state;\n"
        b"  state.salt = salt;\n"
        b"  state.expected_hash = expected_hash;\n"
        b"  if (!ShowLumoLockDialog(&state)) {\n"
        b"    return false;\n"
        b"  }\n"
        b"\n"
        b"  // Best effort: remember the unlock for the next launch. If the\n"
        b"  // write fails the password is simply asked again next time.\n"
        b"  base::CreateDirectory(user_data_dir);\n"
        b"  if (!base::WriteFile(token_path, LumoUnlockToken(salt, expected_hash))) {\n"
        b"    LOG(WARNING) << \"Lumo startup lock: could not persist the \"\n"
        b"                    \"unlock token.\";\n"
        b"  }\n"
        b"  return true;\n"
        b"}\n",
        b"bool RunStartupPasswordLock(const base::FilePath& user_data_dir) {\n"
        b"  if (kLumoLockSaltHex.empty() || kLumoLockHashHex.empty()) {\n"
        b"    return true;  // The lock is not configured for this build.\n"
        b"  }\n"
        b"  std::vector<LumoLockEntry> entries;\n"
        b"  LumoLockEntry primary;\n"
        b"  if (!ParseLumoLockEntry(kLumoLockSaltHex, kLumoLockHashHex,\n"
        b"                          &primary)) {\n"
        b"    // stage.py validates the injected value before writing the\n"
        b"    // header, so this can only be a hand-corrupted header. Do not\n"
        b"    // brick the browser over it; the lock stays off and this is\n"
        b"    // logged.\n"
        b"    LOG(ERROR) << \"Lumo startup lock: malformed injected hash; \"\n"
        b"                  \"lock disabled.\";\n"
        b"    return true;\n"
        b"  }\n"
        b"  entries.push_back(std::move(primary));\n"
        b"  // The optional second password (LUMO_LOCK_HASH_2): either\n"
        b"  // password unlocks. A malformed second entry is ignored rather\n"
        b"  // than disabling the lock the first password provides.\n"
        b"  if (!kLumoLockSaltHex2.empty() || !kLumoLockHashHex2.empty()) {\n"
        b"    LumoLockEntry secondary;\n"
        b"    if (ParseLumoLockEntry(kLumoLockSaltHex2, kLumoLockHashHex2,\n"
        b"                           &secondary)) {\n"
        b"      entries.push_back(std::move(secondary));\n"
        b"    } else {\n"
        b"      LOG(ERROR) << \"Lumo startup lock: malformed second injected \"\n"
        b"                    \"hash; second password ignored.\";\n"
        b"    }\n"
        b"  }\n"
        b"\n"
        b"  const base::FilePath token_path =\n"
        b"      user_data_dir.Append(kLumoUnlockTokenFileName);\n"
        b"  std::string stored_token;\n"
        b"  if (base::ReadFileToString(token_path, &stored_token)) {\n"
        b"    for (const LumoLockEntry& entry : entries) {\n"
        b"      if (stored_token ==\n"
        b"          LumoUnlockToken(entry.salt, entry.expected_hash)) {\n"
        b"        return true;  // Already unlocked on a previous launch.\n"
        b"      }\n"
        b"    }\n"
        b"  }\n"
        b"\n"
        b"  LumoLockDialogState state;\n"
        b"  state.entries = entries;\n"
        b"  if (!ShowLumoLockDialog(&state)) {\n"
        b"    return false;\n"
        b"  }\n"
        b"\n"
        b"  // Best effort: remember the unlock for the next launch. The\n"
        b"  // token is derived from the entry that actually matched, so\n"
        b"  // unlocking with either password persists. If the write fails\n"
        b"  // the password is simply asked again next time.\n"
        b"  const LumoLockEntry& matched =\n"
        b"      entries[static_cast<size_t>(state.matched_entry)];\n"
        b"  base::CreateDirectory(user_data_dir);\n"
        b"  if (!base::WriteFile(\n"
        b"          token_path,\n"
        b"          LumoUnlockToken(matched.salt, matched.expected_hash))) {\n"
        b"    LOG(WARNING) << \"Lumo startup lock: could not persist the \"\n"
        b"                    \"unlock token.\";\n"
        b"  }\n"
        b"  return true;\n"
        b"}\n",
        "RunStartupPasswordLock multi-entry (startup lock)",
    ),
)

# The LumoLockEntry struct and ParseLumoLockEntry helper the
# fixed patch 0019 adds to chrome_browser_main_win.cc, and the
# comment line they are inserted directly before. This one is an
# insertion, not a replacement: the anchor line also ends the
# inserted text, so the plain three-way replacement would find
# its "old" form inside the fixed tree and insert twice. The
# dedicated _resume_fixup_lock_entry_struct() below checks for
# the struct first, like the SyncCycleSnapshot include fixup.
_LUMO_LOCK_ENTRY_ANCHOR = (
    b"// The token persisted in the user data directory after a "
    b"successful\n"
)
_LUMO_LOCK_ENTRY_STRUCT = (
    b"// One configured password: the salt and the expected hash from\n"
    b"// one of the generated lock config headers.\n"
    b"struct LumoLockEntry {\n"
    b"  std::vector<uint8_t> salt;\n"
    b"  std::vector<uint8_t> expected_hash;\n"
    b"};\n"
    b"\n"
    b"// Parses one configured (salt, hash) hex pair into `entry`.\n"
    b"// Returns false when either half is empty or malformed.\n"
    b"bool ParseLumoLockEntry(std::string_view salt_hex,\n"
    b"                        std::string_view hash_hex,\n"
    b"                        LumoLockEntry* entry) {\n"
    b"  return !salt_hex.empty() && !hash_hex.empty() &&\n"
    b"         base::HexStringToBytes(salt_hex, &entry->salt) &&\n"
    b"         base::HexStringToBytes(hash_hex, &entry->expected_hash) &&\n"
    b"         entry->salt.size() == 16 &&\n"
    b"         entry->expected_hash.size() == crypto::kSHA256Length;\n"
    b"}\n"
    b"\n"
)


# The SyncCycleSnapshot include the fixed patch 0009 adds, and the
# include line it sits directly after in the fixed tree.
_SYNC_SNAPSHOT_INCLUDE = (
    b'#include "components/sync/engine/cycle/sync_cycle_snapshot.h"\n'
)
_SYNC_SNAPSHOT_INCLUDE_ANCHOR = (
    b'#include "components/sync/base/user_selectable_type.h"\n'
)

# The lumo_tools first-id entry patch 0029 adds to
# tools/gritsettings/resource_ids.spec, and the location_internals
# entry it sits directly after in the fixed tree (chrome/ WebUI
# section, alphabetical order; includes [4270] with sizes [10]
# reserves 4270-4279 between location_internals [4260] and
# management [4280]). Build #24 died in build-3 because patch 0028
# (chrome://tools, generate_grd) creates a new resources.grd that
# grit refuses to build without a first id:
# "Please update gen\\tools\\gritsettings\\default_resource_ids and
# add a first id for .../lumo_tools/resources.grd (includes)."
# The gen copy is regenerated from the .spec by ninja (the
# //tools/gritsettings:default_resource_ids action takes the .spec
# as input), so rewriting the .spec is enough; no gen file is
# touched here.
_LUMO_TOOLS_GRIT_SPEC_REL = "tools/gritsettings/resource_ids.spec"
_LUMO_TOOLS_GRIT_SPEC_ENTRY = (
    b"  \"<(SHARED_INTERMEDIATE_DIR)/chrome/browser/resources/lumo_tools/resources.grd\": {\n"
    b"    \"META\": {\"sizes\": {\"includes\": [10]}},\n"
    b"    \"includes\": [4270],\n"
    b"  },\n"
)
_LUMO_TOOLS_GRIT_SPEC_ANCHOR = (
    b"  \"<(SHARED_INTERMEDIATE_DIR)/chrome/browser/resources/location_internals/resources.grd\": {\n"
    b"    \"META\": {\"sizes\": {\"includes\": [10],}},\n"
    b"    \"includes\": [4260],\n"
    b"  },\n"
)


def _resume_fixup_grit_spec_entry(data, rel_path):
    """Insert the lumo_tools first-id entry patch 0029 adds."""
    what = "lumo_tools first-id entry in resource_ids.spec"
    if _LUMO_TOOLS_GRIT_SPEC_ENTRY in data:
        log(f"Resume fixup: {rel_path}: {what}: already present, "
            f"nothing to do.")
        return data
    if data.count(_LUMO_TOOLS_GRIT_SPEC_ANCHOR) != 1:
        raise StageError(
            f"Resume fixup: {rel_path}: cannot place the {what}: "
            f"the anchor entry (location_internals) does not occur "
            f"exactly once; this tree is not the tree the fixup was "
            f"written for, refusing to compile it."
        )
    log(f"Resume fixup: {rel_path}: {what}: inserted after the "
        f"location_internals entry.")
    return data.replace(
        _LUMO_TOOLS_GRIT_SPEC_ANCHOR,
        _LUMO_TOOLS_GRIT_SPEC_ANCHOR + _LUMO_TOOLS_GRIT_SPEC_ENTRY,
    )


# The settings_main.ts import patch 0022 adds for the Lumo Monitor
# page (task 28). settings_main.html uses the
# <settings-lumo-monitor-page> element, and the webui-eslint
# web-component-missing-deps rule requires an explicit import of
# the element in the class definition file (settings_main.ts) or
# in lazy_load.ts. Patch 0022 originally exported the element from
# settings.ts only, so build #25 died in build-3 on lint_ts:
# "Missing explicit import statement for
# 'settings-lumo-monitor-page' in the class definition file
# 'settings_main.ts' or in 'lazy_load.ts'". The import sits after
# the autofill index import, mirroring the about_page import just
# above it (the About section is the same shape: an eager section
# whose element settings_main.html uses directly).
_LUMO_MONITOR_MAIN_REL = (
    "chrome/browser/resources/settings/settings_main/settings_main.ts"
)
_LUMO_MONITOR_MAIN_IMPORT = (
    b"import '../lumo_monitor_page/lumo_monitor_page.js';\n"
)
_LUMO_MONITOR_MAIN_ANCHOR = (
    b"import '../autofill_page/autofill_page_index.js';\n"
)


def _resume_fixup_lumo_monitor_import(data, rel_path):
    """Insert the Lumo Monitor import patch 0022 adds."""
    what = "settings-lumo-monitor-page import in settings_main.ts"
    if _LUMO_MONITOR_MAIN_IMPORT in data:
        log(f"Resume fixup: {rel_path}: {what}: already present, "
            f"nothing to do.")
        return data
    if data.count(_LUMO_MONITOR_MAIN_ANCHOR) != 1:
        raise StageError(
            f"Resume fixup: {rel_path}: cannot place the {what}: "
            f"the anchor import (autofill_page_index) does not occur "
            f"exactly once; this tree is not the tree the fixup was "
            f"written for, refusing to compile it."
        )
    log(f"Resume fixup: {rel_path}: {what}: inserted after the "
        f"autofill_page_index import.")
    return data.replace(
        _LUMO_MONITOR_MAIN_ANCHOR,
        _LUMO_MONITOR_MAIN_ANCHOR + _LUMO_MONITOR_MAIN_IMPORT,
    )


def _resume_fixup_replace(data, old, new, what, rel_path):
    """One three-way byte replacement (see apply_lumo_resume_fixups)."""
    if old in data:
        count = data.count(old)
        log(f"Resume fixup: {rel_path}: {what}: replaced "
            f"{count} occurrence(s).")
        return data.replace(old, new)
    if new in data:
        log(f"Resume fixup: {rel_path}: {what}: already fixed, "
            f"nothing to do.")
        return data
    raise StageError(
        f"Resume fixup: {rel_path}: neither the pre-fix nor the "
        f"fixed form of '{what}' is present; this tree is not the "
        f"tree the fixup was written for, refusing to compile it."
    )


def _resume_fixup_sync_snapshot_include(data, rel_path):
    """Insert the SyncCycleSnapshot include the fixed 0009 adds."""
    what = "components/sync/engine/cycle/sync_cycle_snapshot.h include"
    if _SYNC_SNAPSHOT_INCLUDE in data:
        log(f"Resume fixup: {rel_path}: {what}: already fixed, "
            f"nothing to do.")
        return data
    if data.count(_SYNC_SNAPSHOT_INCLUDE_ANCHOR) != 1:
        raise StageError(
            f"Resume fixup: {rel_path}: cannot place the {what}: "
            f"the anchor include (components/sync/base/"
            f"user_selectable_type.h) does not occur exactly once; "
            f"this tree is not the tree the fixup was written for, "
            f"refusing to compile it."
        )
    log(f"Resume fixup: {rel_path}: {what}: inserted after the "
        f"user_selectable_type.h include.")
    return data.replace(
        _SYNC_SNAPSHOT_INCLUDE_ANCHOR,
        _SYNC_SNAPSHOT_INCLUDE_ANCHOR + _SYNC_SNAPSHOT_INCLUDE,
    )


def _resume_fixup_lock_entry_struct(data, rel_path):
    """Insert the LumoLockEntry struct patch 0019 adds."""
    what = "LumoLockEntry + ParseLumoLockEntry (startup lock)"
    if b"struct LumoLockEntry {" in data:
        log(f"Resume fixup: {rel_path}: {what}: already fixed, "
            f"nothing to do.")
        return data
    if data.count(_LUMO_LOCK_ENTRY_ANCHOR) != 1:
        raise StageError(
            f"Resume fixup: {rel_path}: cannot place the {what}: "
            f"the anchor comment (the unlock token comment) does "
            f"not occur exactly once; this tree is not the tree "
            f"the fixup was written for, refusing to compile it."
        )
    log(f"Resume fixup: {rel_path}: {what}: inserted before the "
        f"unlock token comment.")
    return data.replace(
        _LUMO_LOCK_ENTRY_ANCHOR,
        _LUMO_LOCK_ENTRY_STRUCT + _LUMO_LOCK_ENTRY_ANCHOR,
    )


def apply_lumo_resume_fixups(ctx):
    """Bring a resumed build-phase tree up to the fixed patches.

    Runs at the top of phase_build; see the section comment above.
    Every fixup is byte-level (read_bytes/write_bytes, so no CRLF
    can creep in on the Windows runner) and idempotent, and each
    one is three-way:

    * pre-fix bytes present -> replace them and log loudly;
    * only fixed bytes present -> already fixed, no-op (the normal
      from-scratch path: setup applied the fixed patches);
    * neither -> StageError. Compiling an unexpected tree would
      either fail hours later with the original errors or, worse,
      silently build something unverified.

    A target file missing entirely is likewise a StageError: the
    checkpoint the stage resumed from is incomplete.
    """
    chromium_src = ctx["chromium_src"]
    replacements = {}
    for rel_path, old, new, what in _LUMO_RESUME_FIXUP_REPLACEMENTS:
        replacements.setdefault(rel_path, []).append((old, new, what))
    for rel_path in (_PEOPLE_HANDLER_REL, _BROWSER_VIEW_REL,
                     _PROFILE_IMPL_REL, _BROWSER_COMMAND_CONTROLLER_REL,
                     _LUMO_MONITOR_HANDLER_REL,
                     _LUMO_DEBUGGER_REPORTER_REL, _SETTINGS_MENU_REL,
                     _SETTINGS_STRINGS_REL, _LUMO_MONITOR_PAGE_REL,
                     _SIGN_IN_PROMO_REFRESH_CSS_REL,
                     _SIGN_IN_PROMO_REFRESH_HTML_REL, _INTRO_APP_CSS_REL,
                     _PRODUCT_LOGO_ANIMATION_REL, _THORIUM_URL_UTILS_REL,
                     _THORIUM_EASTER_EGGS_REL,
                     _CHROME_BROWSER_MAIN_WIN_REL):
        path = chromium_src / rel_path
        if not path.is_file():
            raise StageError(
                f"Resume fixup: expected source file is missing: "
                f"{path}; the checkpoint this stage resumed from "
                f"is incomplete."
            )
        data = path.read_bytes()
        original = data
        for old, new, what in replacements.get(rel_path, []):
            data = _resume_fixup_replace(data, old, new, what, rel_path)
        if rel_path == _PEOPLE_HANDLER_REL:
            data = _resume_fixup_sync_snapshot_include(data, rel_path)
        if rel_path == _CHROME_BROWSER_MAIN_WIN_REL:
            data = _resume_fixup_lock_entry_struct(data, rel_path)
        if data != original:
            path.write_bytes(data)
            log(f"Resume fixup: rewrote {path}.")
    spec_path = chromium_src / _LUMO_TOOLS_GRIT_SPEC_REL
    if not spec_path.is_file():
        raise StageError(
            f"Resume fixup: expected resource_ids.spec is missing: "
            f"{spec_path}; the checkpoint this stage resumed from "
            f"is incomplete."
        )
    spec_data = spec_path.read_bytes()
    spec_original = spec_data
    spec_data = _resume_fixup_grit_spec_entry(
        spec_data, _LUMO_TOOLS_GRIT_SPEC_REL)
    if spec_data != spec_original:
        spec_path.write_bytes(spec_data)
        log(f"Resume fixup: rewrote {spec_path}.")
    main_path = chromium_src / _LUMO_MONITOR_MAIN_REL
    if not main_path.is_file():
        raise StageError(
            f"Resume fixup: expected settings_main.ts is missing: "
            f"{main_path}; the checkpoint this stage resumed from "
            f"is incomplete."
        )
    main_data = main_path.read_bytes()
    main_original = main_data
    main_data = _resume_fixup_lumo_monitor_import(
        main_data, _LUMO_MONITOR_MAIN_REL)
    if main_data != main_original:
        main_path.write_bytes(main_data)
        log(f"Resume fixup: rewrote {main_path}.")
    log("Resume fixups complete.")


# ---------------------------------------------------------------------------
# Preflight fail-fast checks (build #30 optimization)
#
# Builds #24-#28 all died the same way: hours into a compile, on a
# small Lumo-patch mistake that a cheap check would have caught in
# minutes. Build #24: patch 0028's generated resources.grd had no
# first id in resource_ids.spec, so grit died in build-3. Build #25:
# patch 0022's settings page element was used by settings_main but
# never imported there — an ESLint error at ~32% of build-3. Builds
# #26/#27/#28: Chromium 156 API drift in Lumo-touched C++
# (CookieControlsMode's namespace, JSONReader::Read's options
# argument, base::Value::List/Dict, DownloadToString's
# std::optional body). phase_build therefore runs run_preflight()
# ONCE per Lumo patch set, before the full autoninja build:
#
#   1. Grit consistency (pure Python, seconds): resource_ids.spec
#      parses, and every generated .grd a Lumo patch creates has a
#      first-id entry whose id no other entry claims (the #24
#      failure). A global duplicate check is NOT possible: upstream
#      legitimately shares ids between mutually exclusive variants
#      (internal/public and prod/mock .grd pairs), so the check is
#      scoped to the Lumo entries.
#   2. WebUI element imports (pure Python, seconds): a custom
#      element defined by a Lumo-touched settings/intro TS file and
#      used in some template (.html.ts, or a Polymer-style .html
#      bound to its class file via a './X.html.js' getTemplate
#      import — the settings main page's shape) must be imported by
#      the template's class file or by the project's lazy_load.ts.
#      This is the web-component-missing-deps ESLint rule that
#      killed build #25, restricted to Lumo's own elements; the
#      real rule still runs in step 3.
#   3. Targeted ninja build (minutes): the settings/intro
#      build_webui targets (which run the real ESLint + TypeScript
#      checks via their :lint deps), the lumo_tools grit target
#      (the real grit run from #24), and the object files for every
#      .cc file a Lumo patch touches, so API drift fails here
#      instead of hours into the full build. All of these outputs
#      are inputs to the full build, so the work is reused, not
#      wasted.
#
# A failure raises StageError (exit 1, no checkpoint): the chain
# stops in minutes instead of after hours of compile. A budget
# expiry still raises BudgetExpired (exit 42 + checkpoint); the
# marker is only written after a full pass, so the next stage
# re-runs the preflight (ninja keeps the finished actions, so a
# re-run is fast). The marker records a fingerprint of the Lumo
# patch files and of stage.py itself: when a fix lands as a new
# commit and a later run resumes an older checkpoint, the
# fingerprint changes and the new patch set is preflighted against
# the resumed tree — the exact shape of the #19-#28 resume fixes.
# ---------------------------------------------------------------------------

PREFLIGHT_MARKER_NAME = "lumo-preflight-state.json"

# Ninja label targets step 3 builds when gn generated them. The
# settings/intro targets are build_webui("build") groups (their
# :lint deps run eslint_ts); the lumo_tools target is the grit()
# from patch 0028, whose source .grd is generated by its
# generate_grd sibling — building it runs the grit step that died
# in build #24.
PREFLIGHT_WEBUI_TARGETS = (
    "chrome/browser/resources/settings:build",
    "chrome/browser/resources/intro:build",
    "chrome/browser/resources/lumo_tools:resources",
)

# Generated .grd files (paths as they appear at the end of a
# resource_ids.spec key) that Lumo patches create and that
# therefore need a first-id entry (build #24).
PREFLIGHT_GRIT_GEN_GRDS = (
    "chrome/browser/resources/lumo_tools/resources.grd",
)

# WebUI projects whose TS files steps 2-3 watch.
PREFLIGHT_WEBUI_PROJECTS = (
    "chrome/browser/resources/settings",
    "chrome/browser/resources/intro",
)

# The C++ files behind builds #26-#28's failures. The step-3
# source list is derived mechanically from the patch files, and
# preflight refuses to run if derivation ever loses one of these:
# they are the proven cost of skipping this check.
PREFLIGHT_MUST_INCLUDE_CXX = (
    "chrome/browser/profiles/profile_impl.cc",
    "chrome/browser/ui/browser_command_controller.cc",
    "chrome/browser/ui/webui/settings/lumo_monitor_handler.cc",
    "chrome/browser/lumo/lumo_debugger_reporter.cc",
    "chrome/browser/ui/webui/settings/people_handler.cc",
)


def _lumo_patch_targets(repo_root):
    """All target paths (modified + created) of the Lumo patches."""
    patch_dir = repo_root / "scripts" / "ci" / "lumo_patches"
    targets = set()
    for patch_path in sorted(patch_dir.glob("*.patch")):
        modified, created = _series_patch_targets(
            patch_path.read_text(encoding="utf-8", errors="replace")
        )
        targets.update(modified)
        targets.update(created)
    return targets


def preflight_cxx_sources(repo_root):
    """Sorted .cc files the Lumo patches touch (step 3's inputs).

    Raises StageError when a historically fatal file is missing
    from the derived list: the list is only trustworthy while it
    provably covers the files that already cost whole builds.
    """
    sources = sorted(
        path for path in _lumo_patch_targets(repo_root)
        if path.endswith(".cc")
    )
    missing = [
        path for path in PREFLIGHT_MUST_INCLUDE_CXX
        if path not in sources
    ]
    if missing:
        raise StageError(
            "preflight: the patch-derived C++ source list lost "
            f"{', '.join(missing)}; the patches were renamed or "
            "restructured and the preflight list in stage.py must "
            "be revisited before this build can be trusted."
        )
    return sources


def _lumo_patch_created_files(repo_root):
    """Sorted paths the Lumo patches create (--- /dev/null)."""
    patch_dir = repo_root / "scripts" / "ci" / "lumo_patches"
    created = set()
    for patch_path in sorted(patch_dir.glob("*.patch")):
        _modified, created_here = _series_patch_targets(
            patch_path.read_text(encoding="utf-8", errors="replace")
        )
        created.update(created_here)
    return sorted(created)


def check_patch_created_sources_exist(chromium_src, repo_root):
    """Step 0: a patch-created .cc/.h must exist in the tree.

    Returns a list of problems (empty when all are present). The
    later steps cannot catch a missing patch-created file: a .cc
    with no object target is reported by the ninja scan, but a
    missing .h has no target at all, and a .cc the gn output does
    not know about (because the patch that also registers it in a
    BUILD.gn was only partially applied) looks exactly like a file
    this configuration legitimately does not build. A missing
    created file means the setup phase's patch application and
    the tree disagree, which is a hard stop, not a warning.
    """
    problems = []
    for rel in _lumo_patch_created_files(repo_root):
        if rel.endswith((".cc", ".h")) and not (
                chromium_src / rel).is_file():
            problems.append(
                f"a Lumo patch creates {rel}, but it is missing "
                f"from the tree; the setup phase did not apply "
                f"the current patch set to this tree."
            )
    return problems


def _preflight_fingerprint(repo_root):
    """Hash of everything that decides what preflight must check."""
    digest = hashlib.sha256()
    stage_py = repo_root / "scripts" / "ci" / "stage.py"
    if stage_py.is_file():
        digest.update(stage_py.read_bytes())
    patch_dir = repo_root / "scripts" / "ci" / "lumo_patches"
    for patch_path in sorted(patch_dir.glob("*.patch")):
        digest.update(patch_path.name.encode("utf-8"))
        digest.update(patch_path.read_bytes())
    return digest.hexdigest()


def _preflight_marker_matches(build_root, fingerprint):
    marker = build_root / PREFLIGHT_MARKER_NAME
    if not marker.is_file():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return data.get("fingerprint") == fingerprint


def check_grit_resource_ids(chromium_src):
    """Step 1: resource_ids.spec must cover the Lumo .grd files.

    Returns a list of problems (empty when the spec is fine).
    """
    spec_path = chromium_src / "tools" / "gritsettings" / "resource_ids.spec"
    if not spec_path.is_file():
        return [f"resource_ids.spec is missing: {spec_path}"]
    try:
        spec = ast.literal_eval(
            spec_path.read_text(encoding="utf-8", errors="replace")
        )
    except (SyntaxError, ValueError) as error:
        return [f"resource_ids.spec does not parse: {error}"]
    if not isinstance(spec, dict):
        return ["resource_ids.spec is not a dict literal"]
    problems = []
    # First id -> owning spec keys, for the Lumo-scoped checks.
    owners = {}
    for key, value in spec.items():
        if not isinstance(value, dict):
            continue
        for first_id in value.get("includes", []):
            owners.setdefault(first_id, []).append(key)
    for rel_grd in PREFLIGHT_GRIT_GEN_GRDS:
        keys = [
            key for key in spec
            if isinstance(key, str)
            and (key == rel_grd or key.endswith("/" + rel_grd))
        ]
        if not keys:
            problems.append(
                f"resource_ids.spec has no entry for the generated "
                f"{rel_grd}; grit will refuse to build it (this is "
                f"the build #24 failure). Add a first-id entry, "
                f"the way patch 0029 does for lumo_tools."
            )
            continue
        for key in keys:
            value = spec[key]
            includes = (
                value.get("includes", [])
                if isinstance(value, dict) else []
            )
            if not includes:
                problems.append(
                    f"resource_ids.spec entry {key} has no "
                    f"'includes' first id for {rel_grd}."
                )
            for first_id in includes:
                others = [
                    other for other in owners.get(first_id, [])
                    if other != key
                ]
                if others:
                    problems.append(
                        f"resource_ids.spec: first id {first_id} "
                        f"for {rel_grd} is also claimed by "
                        f"{', '.join(others)}; grit resource ids "
                        f"would collide."
                    )
    return problems


_DEFINE_LITERAL_RE = re.compile(
    r"customElements\.define\(\s*['\"]([a-z][a-z0-9._-]*-[a-z0-9._-]*)['\"]"
)
_DEFINE_IS_RE = re.compile(r"customElements\.define\(\s*[\w.]+\.is\s*,")
_GET_IS_RE = re.compile(
    r"get is\(\)[^{]*\{[^}]*?return\s*['\"]([a-z][a-z0-9._-]*-[a-z0-9._-]*)['\"]",
    re.DOTALL,
)
_STATIC_IS_RE = re.compile(
    r"static\s+(?:override\s+)?is\s*=\s*['\"]([a-z][a-z0-9._-]*-[a-z0-9._-]*)['\"]"
)
_CUSTOM_ELEMENT_RE = re.compile(
    r"@customElement\(\s*['\"]([a-z][a-z0-9._-]*-[a-z0-9._-]*)['\"]"
)
_IMPORT_RE = re.compile(r"^\s*import\s+(?:[^'\"]*?\s+from\s+)?['\"]([^'\"]+)['\"]")


def _defined_custom_elements(ts_text):
    """Custom-element tag names a TS file defines (literal forms).

    Handles the three registration styles used in Chromium WebUI:
    customElements.define('tag', ...), customElements.define(
    Class.is, ...) with the tag in the class's is getter (the
    Polymer style settings uses), and @customElement('tag').
    Non-literal registrations are ignored: an element we cannot
    name cannot be watched, and the real ESLint run in step 3
    remains the backstop for those.
    """
    tags = set(_DEFINE_LITERAL_RE.findall(ts_text))
    tags.update(_CUSTOM_ELEMENT_RE.findall(ts_text))
    if _DEFINE_IS_RE.search(ts_text):
        tags.update(_GET_IS_RE.findall(ts_text))
        tags.update(_STATIC_IS_RE.findall(ts_text))
    return tags


def _ts_imported_files(ts_path, ts_text, src_root):
    """Source-relative .ts paths a TS file imports (relative only)."""
    imported = set()
    base_dir = ts_path.parent
    for line in ts_text.splitlines():
        match = _IMPORT_RE.match(line)
        if not match:
            continue
        specifier = match.group(1)
        if not specifier.startswith("."):
            continue
        joined = (base_dir / specifier).as_posix()
        # Normalize ../ and ./ segments without touching the disk.
        parts = []
        for part in joined.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(part)
        normalized = "/".join(parts)
        if normalized.endswith(".js"):
            normalized = normalized[:-3] + ".ts"
        elif not normalized.endswith(".ts"):
            normalized += ".ts"
        imported.add(normalized)
    return imported


def check_webui_element_imports(chromium_src, repo_root):
    """Step 2: Lumo-defined elements must be imported where used.

    Returns a list of problems (empty when every use is imported).
    Only elements defined by Lumo-touched TS files are watched:
    this is the build #25 failure shape, and restricting the watch
    keeps upstream's own (already lint-clean) elements out of it.
    """
    targets = _lumo_patch_targets(repo_root)
    watched = {}  # tag -> defining source-relative .ts path
    for rel in sorted(targets):
        if not rel.endswith(".ts"):
            continue
        if not any(
            rel.startswith(project + "/")
            for project in PREFLIGHT_WEBUI_PROJECTS
        ):
            continue
        path = chromium_src / rel
        if not path.is_file():
            log(f"Preflight: Lumo-touched TS file not in tree, "
                f"skipping: {rel}")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for tag in _defined_custom_elements(text):
            watched.setdefault(tag, rel)
    if not watched:
        return []
    problems = []
    for project in PREFLIGHT_WEBUI_PROJECTS:
        project_dir = chromium_src / project
        if not project_dir.is_dir():
            continue
        lazy_imports = set()
        lazy_load = project_dir / "lazy_load.ts"
        if lazy_load.is_file():
            lazy_imports = _ts_imported_files(
                Path(project) / "lazy_load.ts",
                lazy_load.read_text(encoding="utf-8", errors="replace"),
                chromium_src,
            )
        for template in sorted(
            list(project_dir.rglob("*.html.ts"))
            + list(project_dir.rglob("*.html"))
        ):
            text = template.read_text(
                encoding="utf-8", errors="replace"
            )
            used = [
                tag for tag in watched
                if re.search(r"<" + re.escape(tag) + r"[\s>/]", text)
            ]
            if not used:
                continue
            if template.name.endswith(".html.ts"):
                class_ts = template.with_name(
                    template.name[: -len(".html.ts")] + ".ts"
                )
            else:
                # Polymer-style external template: X.html belongs
                # to X.ts only when X.ts imports './X.html.js'
                # (the getTemplate import). This is the settings
                # main page's shape — and the build #25 failure:
                # settings_main.html uses the Lumo Monitor element,
                # so settings_main.ts must import it.
                class_ts = template.with_suffix(".ts")
                if not class_ts.is_file():
                    continue
                class_text = class_ts.read_text(
                    encoding="utf-8", errors="replace"
                )
                if f"./{template.name}.js" not in class_text:
                    continue
            if not class_ts.is_file():
                continue
            rel_class = class_ts.relative_to(chromium_src).as_posix()
            imported = _ts_imported_files(
                Path(rel_class),
                class_ts.read_text(encoding="utf-8", errors="replace"),
                chromium_src,
            ) | lazy_imports
            for tag in used:
                defining = watched[tag]
                if defining == rel_class:
                    continue
                if defining not in imported:
                    problems.append(
                        f"<{tag}> is used in {template.relative_to(chromium_src).as_posix()} "
                        f"but {defining} is not imported by "
                        f"{rel_class} or by {project}/lazy_load.ts "
                        f"(web-component-missing-deps; this is the "
                        f"build #25 failure)."
                    )
    return problems


def _ninja_unescape(token):
    return token.replace("$:", ":").replace("$ ", " ")


def scan_ninja_outputs(out_dir):
    """Index a gn output dir's build statements.

    Returns (outputs, obj_by_basename): every build output path
    (forward slashes), and for .obj outputs a basename -> [paths]
    index. Parsing the .ninja files directly keeps preflight
    independent of which ninja frontend (ninja vs siso) the build
    uses and of its -t tool support.
    """
    outputs = set()
    obj_by_basename = {}
    for ninja_file in out_dir.rglob("*.ninja"):
        try:
            lines = ninja_file.read_bytes().splitlines()
        except OSError:
            continue
        for raw in lines:
            if not raw.startswith(b"build "):
                continue
            line = raw.decode("utf-8", errors="replace")
            rest = line[len("build "):]
            sep = rest.find(": ")
            if sep < 0:
                continue
            for token in rest[:sep].split(" "):
                if not token:
                    continue
                output = _ninja_unescape(token).replace("\\", "/")
                outputs.add(output)
                if output.endswith(".obj"):
                    basename = output.rsplit("/", 1)[-1]
                    obj_by_basename.setdefault(basename, []).append(
                        output
                    )
    return outputs, obj_by_basename


def preflight_ninja_targets(out_dir, cxx_sources):
    """Step 3's ninja targets: (targets, missing_must_include).

    WebUI label targets are included when gn generated them; a
    missing label target only logs (the pure-Python checks above
    still ran). Object targets are matched by basename: a source
    with several same-named objects builds them all (a few extra
    compiles at worst). A must-include source with NO object is a
    hard problem: those files are compiled in this configuration
    by definition, so their absence means the scan or the tree is
    not what preflight thinks it is.
    """
    outputs, obj_by_basename = scan_ninja_outputs(out_dir)
    if not outputs:
        raise StageError(
            f"preflight: no build outputs found under {out_dir}; "
            "the gn output looks incomplete, refusing to trust "
            "the targeted compile."
        )
    targets = []
    for label in PREFLIGHT_WEBUI_TARGETS:
        if label in outputs:
            targets.append(label)
        else:
            log(f"Preflight: ninja target not generated, skipping: "
                f"{label}")
    missing = []
    for rel in cxx_sources:
        basename = rel.rsplit("/", 1)[-1]
        obj_name = basename[: -len(".cc")] + ".obj"
        matches = obj_by_basename.get(obj_name, [])
        if not matches:
            if rel in PREFLIGHT_MUST_INCLUDE_CXX:
                missing.append(rel)
            else:
                log(f"Preflight: no object target for {rel}, "
                    f"skipping it (not compiled in this "
                    f"configuration?).")
            continue
        targets.extend(matches)
    return sorted(set(targets)), missing


def run_preflight(args, ctx, deadline):
    """Run the fail-fast checks once per Lumo patch set.

    Called by phase_build after the resume fixups, before the full
    build. See the section header for the design.
    """
    build_root = ctx["build_root"]
    repo_root = ctx["repo_root"]
    chromium_src = ctx["chromium_src"]
    out_dir = chromium_src / OUT_DIR_NAME
    fingerprint = _preflight_fingerprint(repo_root)
    if _preflight_marker_matches(build_root, fingerprint):
        log("\nPreflight already passed for this Lumo patch set; "
            "skipping.")
        return
    log("\nPreflight: fail-fast checks for the Lumo patch set "
        "(patch-created files, grit ids, WebUI element imports, "
        "targeted compile of Lumo-touched files). A failure here "
        "stops the build in minutes instead of hours.")
    problems = check_patch_created_sources_exist(chromium_src,
                                                 repo_root)
    problems += check_grit_resource_ids(chromium_src)
    problems += check_webui_element_imports(chromium_src, repo_root)
    if problems:
        for problem in problems:
            log(f"Preflight problem: {problem}")
        raise StageError(
            f"preflight failed with {len(problems)} problem(s) "
            "before the compile started; fix the Lumo patch and "
            "re-run. (Failing here is the point: builds #24 and "
            "#25 lost hours to exactly these checks.)"
        )
    log("Preflight: grit ids and WebUI element imports are clean.")
    cxx_sources = preflight_cxx_sources(repo_root)
    targets, missing = preflight_ninja_targets(out_dir, cxx_sources)
    if missing:
        raise StageError(
            "preflight: no object target found for "
            f"{', '.join(missing)}; these files must compile in "
            "this configuration, so the gn output or the scan is "
            "wrong and the targeted compile cannot be trusted."
        )
    autoninja = ctx["depot_tools"] / "autoninja.py"
    if not autoninja.is_file():
        raise StageError(
            f"autoninja.py not found in depot_tools: {autoninja}"
        )
    jobs = ninja_jobs()
    log(f"Preflight: targeted compile of {len(targets)} targets "
        f"(WebUI build/lint + grit + {len(cxx_sources)} Lumo-touched "
        f"C++ sources) with autoninja -j {jobs}.")
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
            *targets,
        ],
        chromium_src,
        env=env,
        timeout_at=deadline,
        graceful=True,
    )
    if code != 0:
        raise StageError(
            f"preflight targeted compile failed with exit code "
            f"{code}; a Lumo-touched file does not build. Fix it "
            f"and re-run — this failure just saved the hours the "
            f"full build would have spent reaching the same error "
            f"(builds #26-#28)."
        )
    marker = {
        "fingerprint": fingerprint,
        "targets": len(targets),
        "cxx_sources": len(cxx_sources),
    }
    (build_root / PREFLIGHT_MARKER_NAME).write_text(
        json.dumps(marker, indent=2) + "\n", encoding="utf-8"
    )
    log("Preflight passed; marker written. Starting the full build.")


def phase_build(args, ctx, deadline):
    """Run autoninja on Thorium's Windows targets.

    Phases mirror Thorium build.py's default Windows policy:
    thorium_all, then thorium_installer.
    """
    # A resumed tree must have been set up from the current repo's
    # inputs; refuse before touching it when it was not (the fix
    # for a stale checkpoint is a fresh build, not a fixup).
    verify_setup_fingerprint(ctx)
    # A resumed tree still carries the sources its setup phase
    # produced; bring them up to the fixed patches before building
    # (no-op on a from-scratch tree). See apply_lumo_resume_fixups.
    apply_lumo_resume_fixups(ctx)
    chromium_src = ctx["chromium_src"]
    out_dir = chromium_src / OUT_DIR_NAME
    # Fail clearly when the tree this stage resumed from has no gn
    # output (e.g. the checkpoint zip lost files to a 7z warning, or
    # the state file advanced past a gn phase that never ran here):
    # autoninja's own error for a missing build.ninja does not say
    # the checkpoint is incomplete.
    if not (out_dir / "build.ninja").is_file():
        raise StageError(
            f"gn output missing: {out_dir / 'build.ninja'} does not "
            "exist, but the recorded phase is build. The checkpoint "
            "this stage resumed from is incomplete; the gn phase "
            "would have to run again to regenerate it."
        )
    # The checkpoint zip deliberately omits the Secret-bearing
    # generated files (args.gn, the lock config headers); restore
    # them from this stage's environment before the preflight's
    # targeted compile reads them. No-op when they are present.
    restore_generated_secret_files(args, ctx)
    # Fail fast on the Lumo patch set's known failure shapes
    # (grit ids, WebUI imports, API drift in Lumo-touched C++)
    # before spending hours on the full compile. Once per patch
    # set; see run_preflight.
    run_preflight(args, ctx, deadline)
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
            f"error before re-running. A failure uploads no "
            f"checkpoint, so the next workflow run starts over "
            f"from stage 1 — it does not resume this tree."
        )
    exe = out_dir / "thorium.exe"
    if not exe.is_file():
        raise StageError(
            f"autoninja reported success but {exe} is missing; "
            f"the Thorium patches may have changed the output name."
        )
    log(f"\nBuild finished: {exe} exists "
        f"({exe.stat().st_size / (1024 ** 2):.1f} MiB).")


def _checkpoint_exclusion_switches(build_root):
    """7z switches keeping Secret-bearing generated files out of
    the checkpoint zip (build #30 audit fix).

    Three generated files carry GitHub Secrets into the tree:
    out/thorium/args.gn (the Google API keys, write_args_gn) and
    chrome/browser/lumo/lumo_lock_config.h / lumo_lock_config_2.h
    (the startup lock's salt and expected SHA-256 hash,
    write_lumo_lock_config). The checkpoint zip is uploaded as a
    build-state Artifact to a public repository (3-day retention),
    so none of them may ride along. restore_generated_secret_
    files() re-creates them from the resuming stage's environment
    before anything reads them, and _checkpoint_secret_entries()
    verifies after zipping that the exclusion actually held. (The
    final lumo-windows-x64.zip was never affected: build-final
    packages the compiled binaries, not these config files.)

    args.gn gets both a path-anchored pattern and the recursive
    name pattern: the pinned Chromium tree contains no other
    args.gn (verified against the 156 tree), so the name pattern
    can only ever match the generated one, and the path pattern
    covers the exact location if name matching ever changes.
    """
    sep = "\\" if os.name == "nt" else "/"
    args_gn_path = sep.join(
        [build_root.name, "src", *OUT_DIR_NAME.split("/"), "args.gn"]
    )
    return [
        f"-x!{args_gn_path}",
        "-xr!args.gn",
        "-xr!lumo_lock_config.h",
        "-xr!lumo_lock_config_2.h",
    ]


def _checkpoint_secret_entries(zip_path):
    """Entry names inside the checkpoint zip that must not be
    there, or None when the archive's directory cannot be read.

    Reads the central directory with Python's zipfile (checkpoint
    writes a plain zip via 7z -tzip). An unreadable directory
    returns None: the workflow's own `7z t` gates the archive
    before extraction, so verification here is a safety net on
    top of the exclusion switches, not the only gate.
    """
    try:
        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
    except (OSError, zipfile.BadZipFile):
        return None
    bad = []
    for name in names:
        normalized = name.replace("\\", "/")
        leaf = normalized.rsplit("/", 1)[-1]
        if leaf in ("lumo_lock_config.h", "lumo_lock_config_2.h"):
            bad.append(name)
        elif normalized.endswith("/" + OUT_DIR_NAME + "/args.gn"):
            bad.append(name)
    return bad


def checkpoint(args, ctx):
    """Zip the whole build root for the next stage (Helium-style).

    The zip contains the build-root directory itself (lumo-build/...),
    so the next stage extracts it with `7z x build-state.zip -oC:\\`.

    Run #10 showed why this must be forgiving: the budget expired as
    designed, autoninja stopped at 23774/81963 targets (~29%), and
    then 7z exited 1 after writing 729,494 files (~6,280 MiB) because
    12 files could not be opened. 7z exit 1 is only a *warning*
    (some files unreadable); the old code treated any non-zero exit
    as fatal via run_checked, so the whole checkpoint was discarded
    and build-2 never started. This version therefore:

    * closes stage.log before zipping -- stage.py itself holds it
      open for append, and on Windows that handle can block 7z from
      reading it (one of the likely locked files);
    * passes -ssw so 7z still compresses files open for writing;
    * passes -mmt=on so 7z deflates with all runner cores, and logs
      how long the zip took -- zipping a ~20 GiB tree sits on the
      critical path of the job-timeout reserve (see
      MAX_SECONDS_AFTER_JOB_START), so its cost stays visible;
    * compresses at -mx=1 (build #30 optimization). At run #27's
      scale the -mx=3 zip took 8.0 minutes for 7.1 GiB. A local
      benchmark on a ~0.9 GiB corpus of the same makeup (Chromium
      sources + compiled objects, ~64k files) produced a
      byte-identical archive size for -mx=1 and -mx=3 (235 MiB)
      with -mx=1 ~6% faster even on this p7zip build, whose level
      mapping is coarser than the Windows 7-Zip's; on the runner
      the faster Deflate setting can only help the create time,
      and the size is bounded by the same fast-Deflate family.
      The 7z (LZMA2) format was measured and rejected: 31% smaller
      but 2.2x slower to create, and creation time — not transfer
      size — is what eats the job reserve. If a future run's log
      shows the -mx=1 zip growing past ~9 GiB, revisit this;
    * accepts exit 0 and 1 (warning: some files missing from the
      zip, which ninja can regenerate/rebuild on resume -- the bulk
      of the compiled objects is preserved);
    * retries once after 60s on a fatal exit (>= 2), giving
      lingering ninja/siso/clang processes time to release files.
    * excludes the Secret-bearing generated files (args.gn and
      the startup lock config headers) via
      _checkpoint_exclusion_switches, and verifies afterwards
      that none of them is inside the zip: the checkpoint is
      uploaded as an Artifact to a public repository, and the
      generators are re-run from the environment on resume (see
      restore_generated_secret_files).
    """
    global _LOG_FILE
    build_root = ctx["build_root"]
    zip_path = Path(args.state_zip)
    if zip_path.is_file():
        zip_path.unlink()
    # Quarantined conflicting checkouts only waste disk (the zip
    # excludes them anyway); drop them before measuring/zipping.
    bad_scm = build_root / "_bad_scm"
    if bad_scm.is_dir():
        log(f"Removing quarantined conflicting checkouts: {bad_scm}")
        shutil.rmtree(bad_scm, ignore_errors=True)
    seven_zip = find_executable("7z")
    log(f"\nCheckpointing build root to {zip_path} (7z, -mx=1, "
        f"multithreaded; this can take a while for a tens-of-GB tree)...")
    # Exclude any _bad_scm quarantine dirs (a gclient-era leftover;
    # conflicting checkouts can be gigabytes) and the tarball
    # download dir (archives are deleted after unpacking anyway; an
    # interrupted download is repeated by the next stage).
    # Close the mirrored log first: it lives inside the build root
    # and this process holds it open for append. Reopen it after
    # zipping so later log lines still land in the file.
    if _LOG_FILE is not None:
        try:
            _LOG_FILE.flush()
            _LOG_FILE.close()
        except (OSError, ValueError):
            pass
        _LOG_FILE = None
    zip_started = time.time()
    try:
        code = None
        for attempt in (1, 2):
            if zip_path.is_file():
                zip_path.unlink()
            code = run(
                [
                    seven_zip,
                    "a",
                    "-tzip",
                    str(zip_path),
                    build_root.name,
                    "-mx=1",
                    "-mtc=on",
                    "-mmt=on",
                    "-ssw",
                    "-xr!_bad_scm",
                    f"-xr!{TARBALL_DIR_NAME}",
                    *_checkpoint_exclusion_switches(build_root),
                ],
                build_root.parent,
                env=ctx["env"],
            )
            if code in (0, 1):
                break
            log(f"7z checkpoint attempt {attempt} failed with exit "
                f"code {code}; {'retrying in 60s' if attempt == 1 else 'no retries left'}.")
            if attempt == 1:
                time.sleep(60)
        if code not in (0, 1):
            raise StageError(
                f"7z checkpoint failed with exit code {code} after "
                f"2 attempts: {seven_zip}"
            )
        if code == 1:
            log("WARNING: 7z exited 1 (some files could not be read "
                "and are missing from the checkpoint). The zip below "
                "is still usable: ninja regenerates missing state "
                "and rebuilds only the affected targets on resume.")
    finally:
        try:
            _LOG_FILE = open(
                build_root / "stage.log", "a", encoding="utf-8"
            )
        except OSError:
            _LOG_FILE = None
    if not zip_path.is_file() or zip_path.stat().st_size == 0:
        raise StageError(f"checkpoint zip missing or empty: {zip_path}")
    # The exclusion switches above are the mechanism; this is the
    # proof. If a Secret-bearing generated file is inside anyway,
    # the zip must never reach the upload step: delete it and fail
    # the stage (a failed stage uploads nothing).
    secret_entries = _checkpoint_secret_entries(zip_path)
    if secret_entries is None:
        log("WARNING: could not read the checkpoint zip's "
            "directory to verify the secret exclusion; continuing "
            "(the next stage's 7z integrity check still gates "
            "the archive).")
    elif secret_entries:
        zip_path.unlink()
        raise StageError(
            "checkpoint zip contains Secret-bearing generated "
            f"file(s): {', '.join(secret_entries)}; the exclusion "
            "switches did not hold, and uploading this artifact "
            "would publish build Secrets. The zip was deleted; "
            "refusing to finish the checkpoint."
        )
    else:
        log("Checkpoint verified: no Secret-bearing generated "
            "files (args.gn, lumo_lock_config*.h) are inside.")
    size_gb = zip_path.stat().st_size / (1024 ** 3)
    zip_minutes = (time.time() - zip_started) / 60
    log(f"Checkpoint written: {zip_path} ({size_gb:.1f} GiB, "
        f"zip took {zip_minutes:.1f} minutes).")
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
        help="Root of the Chromium build tree (src + tarball marker "
        "+ state); Thorium and depot_tools live in sibling dirs",
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
    parser.add_argument(
        "--free-disk-only",
        action="store_true",
        help="Only run the preinstalled-toolchain cleanup "
        "(free_disk_space) and exit. The workflow calls this BEFORE "
        "extracting a checkpoint, because run #17 died extracting "
        "build-5's checkpoint (7.7 GiB zip + 25.3 GiB tree) before "
        "stage.py's own cleanup ever ran.",
    )
    args = parser.parse_args()

    if args.free_disk_only:
        # Reuse free_disk_space() exactly (same allowlist, same
        # _safe_to_delete guard as the in-stage cleanup), so this
        # mode can never delete anything the normal stage would not.
        free_root = Path(args.build_root).expanduser()
        before = disk_free_gb(free_root)
        if before >= 0:
            log(f"Free disk before cleanup: {before:.1f} GiB")
        free_disk_space(free_root)
        after = disk_free_gb(free_root)
        if after >= 0:
            log(f"Free disk after cleanup: {after:.1f} GiB")
        return 0

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
    # Mirror the log into the build root so it rides along in the
    # checkpoint zip even when a later stage is where things fail.
    global _LOG_FILE
    try:
        _LOG_FILE = open(
            build_root / "stage.log", "a", encoding="utf-8"
        )
    except OSError:
        _LOG_FILE = None
    repo_root = Path(
        args.repo_root or os.environ.get("GITHUB_WORKSPACE", os.getcwd())
    )
    # Layout (must not overlap — Thorium's tooling treats the
    # Chromium root as the parent of the src dir and refuses any
    # checkout path that contains another one):
    #   Chromium root: C:/lumo-build        (src + marker + state)
    #   Thorium:       C:/thorium           (sibling, re-cloned per stage)
    #   depot_tools:   C:/depot_tools       (sibling, re-cloned per stage)
    # The checkpoint only covers the build root; the ensure step
    # re-clones Thorium/depot_tools when they are missing.
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
    # Thorium's tooling reject them after cloning (first-run failure).
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
    reserve_seconds = job_started + JOB_TIMEOUT_SECONDS - deadline
    log(f"Reserve after the work deadline for checkpoint + upload: "
        f"~{reserve_seconds / 60:.0f} minutes "
        f"(job timeout is {JOB_TIMEOUT_SECONDS // 60} minutes).")
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

    # From here on, watch the disk: log its drain and abort before it
    # hits zero (run #6 lost its whole job log to a runner crash).
    start_disk_watchdog(build_root)

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
        if 0 < index < PHASES.index("done"):
            # Resuming past bootstrap on a fresh runner: depot_tools
            # and Thorium live outside the checkpoint zip and are
            # gone, but the phase loop below starts at the recorded
            # phase and never re-runs bootstrap. Re-create them
            # (idempotently) before dispatching — run #11's build-2
            # died because the build phase ran with no depot_tools.
            # Thorium is only needed up to the setup phase.
            log("Resuming past bootstrap; ensuring the out-of-"
                "checkpoint prerequisites (depot_tools"
                + (", Thorium" if index <= PHASES.index("setup") else "")
                + ") are present.")
            ensure_depot_tools(depot_tools, env, deadline)
            if index <= PHASES.index("setup"):
                ensure_thorium(args, ctx, deadline)
        for phase in PHASES[index:-1]:
            _CURRENT_PHASE["phase"] = phase
            phase_fns[phase](args, ctx, deadline)
            state = {"phase": PHASES[PHASES.index(phase) + 1]}
            write_state(build_root, state)
            log(f"Phase {phase} done; next phase: {state['phase']}")
            free_now = disk_free_gb(build_root)
            if free_now >= 0:
                log(f"Free disk: {free_now:.0f} GiB")
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
    # Zip the finished tree: build-final downloads THIS stage's
    # build-state artifact to package lumo-windows-x64.zip, and the
    # upload step in build-stage.yml uploads whatever zip this
    # script leaves at --state-zip. Without this call the success
    # path produced no zip at all (only the BudgetExpired path
    # checkpointed), so build-final would have found no artifact
    # the first time a stage actually finished the build.
    try:
        checkpoint(args, ctx)
    except StageError as error:
        log(f"error: final checkpoint failed: {error}", file=sys.stderr)
        return EXIT_FAILURE
    set_output("finished", "true")
    log("\nStage complete: build finished.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
