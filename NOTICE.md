# Notice

`plugin/overlay/`, `plugin/patches/` and `plugin/tests/` are modified files from
**audiobook.koplugin** by stradichenko and contributors:
https://github.com/stradichenko/audiobook.koplugin, tag `v0.6.13`, commit
`9a59606baddc67d31ed801fe20c942c425aed396`, licensed under the GNU Affero General
Public License v3.0 (see `LICENSE`).

Modifications (2026, Mark Phillips):
1. Optional Cloudflare Access service-token headers for Audiobookshelf requests
   (`absclient.lua`, `absbrowse.lua`, `abs_access.example.lua`, `CF-ACCESS-PATCH.md`).
2. Read-along sidecar support for unmodified EPUBs with Audiobookshelf audio
   (`readalongsidecar.lua`, hooks in `main.lua`, `highlightmanager.lua`, `mediasync.lua`,
   `package-for-kobo.sh`, `docs/READALONG_SIDECAR.md`, tests).
3. Fetching `*.readalong.json` stored with an Audiobookshelf item when the item is downloaded.

`plugin/patches/` has each change as a patch against the upstream tag. Everything
else in this repository (`sidecar/`, `fallback/`, `tools/`) is new work and is also
released under AGPL-3.0.

The demo sidecar was made from public-domain sources: *The Gift of the Magi* by O. Henry
(Project Gutenberg) and the LibriVox recording. Those files aren't included.
