# Preinstalled extensions

Assets for the extensions Lumo seeds into newly created profiles
(see `chrome/browser/extensions/preinstalled_extensions.cc` in the
browser tree; the seeding itself is registered by Lumo patches).

## chrome_info_viewer/ + chrome_info_viewer_1.0.0.crx

Chrome Info Viewer is the browser owner's own extension, written
by the owner with AI assistance. It is bundled the same way as
uBlock Origin: stage.py copies the signed CRX into the Chromium
tree (`chrome/browser/resources/chrome_info_viewer/`), the
component resources GRD embeds it, and on first run of a new
profile the browser writes it into the profile and registers it
as an external extension (Lumo patch 0027). Existing profiles are
not seeded — that is the upstream preinstalled-extensions design,
shared with uBlock Origin and uBlock Origin Scope.

- `chrome_info_viewer/` — the extension source, exactly as the
  owner provided it, plus one added manifest field: `"key"`.
- `chrome_info_viewer_1.0.0.crx` — the packed CRX3, produced by
  `pack_chrome_info_viewer.py` (deterministic: rebuilding from
  the same source and key yields byte-identical output).
- `chrome_info_viewer_key.pem` — the RSA private key the CRX is
  signed with. Its ONLY job is to pin the extension ID
  (`cmcleajbmajgneoofpehpkjklnngacna`, derived from the public
  key) so the preinstalled extension keeps the same identity in
  every build. It is not a security boundary: no update URL is
  registered for this extension, the browser only ever installs
  the copy embedded in its own resources, and the key is
  committed here on purpose so the CRX can be rebuilt anywhere.
  Never reuse this key for anything else, and never replace it —
  a different key means a different extension ID.
- `pack_chrome_info_viewer.py` — rebuild / verify tooling
  (Python 3 standard library only). Run it with no arguments to
  rebuild the CRX after editing the source, or with
  `--verify <file.crx>` to check a packed file.

To ship a new version: edit the source, bump `"version"` in its
manifest.json, rebuild (the CRX file name follows the version),
and update the version constant and file names in patch 0027 and
in stage.py's `LUMO_EXTENSION_CRX_COPIES` to match.
