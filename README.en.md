# Lumo — a Thorium-style Chromium fork

A browser built by compiling Chromium itself from source, in the Thorium style.
Instead of using CEF (embedding browser components), it forks the Chromium browser
itself, so — like Chrome — it supports extensions, Widevine, all codecs, and
Google Sync (when using your own API keys).

Based on Chromium 156.0.8078.80 (tracking Thorium's pinned commit).

> This browser was created by AI.

## Status

- The Windows x64 version builds on GitHub Actions and is in daily use on the owner's machine.
- Builds are delivered as Actions Artifacts (`lumo-windows-x64.zip`), not as GitHub Releases.
- `lumo.exe` is self-signed (SmartScreen warnings still appear).

## Features

- **Google Sync**: sign-in and sync work with the owner's own Google API keys.
- **Startup password lock**: a password is requested on first launch only (the hash is injected from Secrets at build time).
- **Lumo Monitor**: a dedicated page in Settings showing the version, profile location, uptime, Sync status, and more.
- **Lumo Labs**: a panel that collects Lumo's own experimental features.
- **Japanese localization**: the item descriptions on chrome://flags are translated into Japanese, and DevTools also defaults to Japanese.
- **lumo:// scheme**: internal pages can be opened with lumo:// in addition to chrome://.
- **Bundled extensions**: uBlock Origin, uBO Scope, and Chrome Info Viewer are automatically installed into new profiles.
- **chrome://tools**: a built-in page collecting tools usable inside the browser.
- **Extra memory saver level**: adds a level that discards tabs left untouched for about 4 minutes.
- A label reading "このブラウザーは試験的な開発段階です" ("This browser is in an experimental development stage") is always shown in the bottom-right corner of the window.
- User data is stored in `%LOCALAPPDATA%\Lumo_Browser\User Data`, kept separate from Thorium and Chrome.

## Target platform and how the build works

- Windows 10/11 64-bit (x64), AVX2 builds only (the owner's PC is an Intel Core i5-8350U, confirmed AVX2-capable; no SSE3 build is made).
- Builds do not run on the owner's PC. They run on GitHub Actions (in the cloud).
- A full Chromium build exceeds GitHub Actions' per-job limit (6 hours), so the
  build is split into consecutive jobs, `build-1` through `build-15`. Each job
  saves its in-progress build state as a checkpoint Artifact, and the next job
  resumes from it. Once every job has finished, the final job packages the result.
- Checkpoints never contain secrets (API keys and the like are excluded from the
  zip, and the zip is inspected for contamination after creation; secrets are
  re-injected from Secrets when a build resumes).
- See [.github/workflows/build-windows.yml](.github/workflows/build-windows.yml) and `BUILDING.md` for details.

## About Google Sync

- Using Sync requires **the owner's own Google API keys**.
- The keys are never committed to this repository. They are stored in GitHub
  Secrets (`GOOGLE_API_KEY` / `GOOGLE_DEFAULT_CLIENT_ID` /
  `GOOGLE_DEFAULT_CLIENT_SECRET`) and injected only at build time. The
  corresponding fields in `args.gn.example` are left blank.
- Keys are created by following Chromium's official "API Keys" instructions (see `BUILDING.md` for details).
- Thorium's keys are not reused. The owner's own keys are used.

## License

Lumo itself is proprietary (All Rights Reserved). Use, copying, and
redistribution are prohibited. See `LICENSE.md` for details.

This repository is public only temporarily, for the purpose of building. It is
planned to be made private once the build is stable. Being public does not
grant any permission to use Lumo.

The underlying Thorium and Chromium, however, remain under the BSD-3-Clause
license, and the licensing of those parts is unchanged.

## Repository layout

| File | Contents |
|---|---|
| `README.md` | The Japanese README |
| `README.en.md` | This file (English README) |
| `BUILDING.md` | Build instructions (in Japanese) |
| `PLAN.md` | List of undecided items and risks |
| `LICENSE.md` | License (Lumo's own parts are All Rights Reserved) |
| `args.gn.example` | Template of GN args for a Windows x64 Release build (API keys left blank) |
| `.github/workflows/` | Build workflows (the caller and the stage runner) |
| `scripts/ci/stage.py` | Script that runs one job's worth of the build |
| `scripts/ci/lumo_patches/` | Lumo's own patch set |
| `scripts/ci/lumo_branding/` | Lumo branding assets (icon, etc.) |
| `scripts/ci/lumo_extensions/` | Bundled extensions shipped with Lumo |
| `scripts/ci/lumo_flag_translations.json` | Japanese translation table for chrome://flags |

The Chromium source code (35–40 GB) is not included in this repository.
It is fetched during CI.

## Android version

An Android version is under development as a separate project in a separate
repository, based on the microG edition of ungoogled-chromium.

## References

- Thorium (Alex313031/thorium) — build procedure, GN args, and patch structure. The current version is Thorium 156.0.8078.80 (Chromium 156).
- Helium-windows (imputnet/helium-windows) — the 15-stage iterative build workflow and the Artifact-based resume mechanism.
