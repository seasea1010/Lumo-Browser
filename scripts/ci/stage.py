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
     Thorium patch series, GRD/XTB rebase.
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

UNVERIFIED (has never run on GitHub Actions):
  * The full build has never completed; every run so far died in
    build-1 on disk space, and later problems (SDK/toolchain
    versions, artifact size limits, patch application on the pinned
    tag, gn args, ninja edge cases) are still waiting behind it.
  * Run #8's tarball acquisition is new. The two archives and their
    SHA-256 hashes were verified locally (2026-11-13) against the
    release's official SHA256SUMS, and their listings contain the
    complete tree (587,942 + 78,486 entries). Unpacked, the combined
    tree measured 14.4 GiB / ~640k files locally. Unpacking uses the
    runner's bsdtar (zstd-capable on Windows Server 2025) with a 7z
    streaming fallback; neither path has run in CI yet.
  * The tarball tree has no .git and no .gclient. Thorium's
    setup.py only checks that src/.git exists and its patch series
    runs `git apply` inside the tree, so bootstrap creates a bare
    `git init` repo there. Whether every Thorium script accepts
    that shim is unverified.
  * Thorium's patch series modifies ~51 files the tarballs do not
    ship (chrome/android/java/** and friends, which only an
    Android checkout carries). phase_setup fetches those from the
    official Chromium mirrors at the pinned tag, and a series
    filter drops any entry whose targets are still absent (a
    safety net). Locally validated 2026-11-13 against the pinned
    Thorium commit: fetch 51/51, filter dropped nothing, and
    setup.py completed end to end on the tarball tree.
  * The build-state zip can be tens of GB. GitHub Artifacts may refuse
    it, and zipping/uploading may eat the time reserve.
  * Renaming thorium.exe to lumo.exe at packaging time is assumed to
    work for a portable build; it has never been tested.
  * Thorium's own branding (About page, installer product name, etc.)
    still says Thorium. Full Lumo rebranding needs separate patches
    (see PLAN.md).
"""

import argparse
import base64
import binascii
import ctypes
import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
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
        log(f"\nCloning depot_tools into {depot_tools}")
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
        series_path.write_text(
            "\n".join(kept_lines) + "\n", encoding="utf-8"
        )
        log(f"Patch series filtered: dropped {len(dropped)} "
            "unappliable entries (see warnings above).")
    return dropped


def phase_setup(args, ctx, deadline):
    """Thorium's setup.py: overlays + patch series + GRD rebase."""
    thorium_root = ctx["thorium_root"]
    chromium_src = ctx["chromium_src"]
    fetch_missing_series_files(
        thorium_root, chromium_src, args.chromium_tag
    )
    fetch_missing_test_fonts(chromium_src, args.chromium_tag)
    filter_unappliable_series(thorium_root, chromium_src)
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
    * accepts exit 0 and 1 (warning: some files missing from the
      zip, which ninja can regenerate/rebuild on resume -- the bulk
      of the compiled objects is preserved);
    * retries once after 60s on a fatal exit (>= 2), giving
      lingering ninja/siso/clang processes time to release files.
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
    log(f"\nCheckpointing build root to {zip_path} (7z, -mx=3; "
        f"this can take a while for a tens-of-GB tree)...")
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
                    "-mx=3",
                    "-mtc=on",
                    "-ssw",
                    "-xr!_bad_scm",
                    f"-xr!{TARBALL_DIR_NAME}",
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
