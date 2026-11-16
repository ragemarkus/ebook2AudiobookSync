# koreader-readalong

Word-by-word and sentence read-along highlighting in **KOReader** for ordinary
**Audiobookshelf (ABS)** audiobooks and the unmodified EPUB of the same book.

You generate a small timing file, `Book.readalong.json`, once per book on a PC
(GPU, using faster-whisper forced alignment). A patched
[audiobook.koplugin](https://github.com/stradichenko/audiobook.koplugin) then
does the rest on the reader:
- plays the ABS audiobook;
- highlights the sentence and, optionally, the word being read;
- turns the pages;
- syncs the position with ABS in both directions.

Requires a real KOReader install (Kobo, Kindle, PocketBook, Android, Linux).
The XTEINK X4 Pro's CrossInk firmware can't run plugins.

| path | what |
|---|---|
| `sidecar/make_readalong_sidecar.py` | Generator. Takes the EPUB and audio tracks and produces the sidecar: faster-whisper word timestamps (CUDA float16 or CPU int8), windowed fuzzy alignment, re-transcription of skipped stretches, then interpolation. `--abs-folder`, `--abs-scan` and `--grimmory-book-id` store the result on the server. |
| `sidecar/publish_sidecar.py` | Stores an existing sidecar with the book: copies it into the ABS item folder and rescans, or uploads it to Grimmory as a supplementary file. |
| `sidecar/abs_fetch.py` | Downloads an ABS item's tracks (in the plugin's order) and its EPUB, then prints the item id and the item's server folder. |
| `sidecar/validate_sidecar.py` | Schema and timing checks, an xpointer check against the EPUB, and an independent audio spot-check. |
| `sidecar/Dockerfile`, `docker-compose.yml` | The generator as a container (GPU and CPU services). |
| `sidecar/SIDECAR_SPEC.md` | The file format, how the plugin finds and uses the file, and where it is stored. |
| `sidecar/demo/` | Prototype sidecar for Gutenberg's *The Gift of the Magi* with the LibriVox reading, plus validation output. The EPUB and audio aren't committed. |
| `plugin/build_plugin.py` | Builds the patched plugin. Downloads the upstream v0.6.13 release (sha256-checked), overlays `plugin/overlay/`, writes `build/audiobook.koplugin.zip`, and runs the Lua tests with `--test`. |
| `plugin/overlay/` | The changed and new plugin files: Cloudflare Access headers, read-along sidecar, and ABS sidecar fetch. |
| `plugin/patches/` | The same changes as `git format-patch` against upstream tag `v0.6.13` (commit `9a59606`). |
| `plugin/tests/` | Lua tests (`dev/` in the plugin). Run them with `build_plugin.py --test` (needs `pip install lupa`). |
| `companion/` | Always-on service for the XPS (Docker): watches ABS, generates missing sidecars (CPU, or an optional G14 GPU worker), validates them and writes them into the ABS item folder. Has a web UI and JSON API. See `companion/README.md`. |
| `fallback/readalong_build.py` | Comparison route: writes the alignment as an EPUB3 Media Overlay book with the audio embedded. |

## Storing the timing file: recommended route

**Put `<Title>.readalong.json` in the audiobook's own folder on the ABS server.**
- Neither Audiobookshelf nor Grimmory has a server plugin system. ABS's is only a proposal (discussion #3744, draft PR #3745). Neither needs one.
- The ABS scanner adds every non-media file in an item's folder to that item's `libraryFiles`. A `.json` gets the file type "metadata", and only `metadata.json`/`metadata.abs` are parsed, so the sidecar is left alone. It can be downloaded with `GET /api/items/<id>/file/<ino>/download`, which the plugin already uses for audio.
- **When the plugin downloads an ABS item, it now also fetches any `*.readalong.json` in that item.** It stores the file in `abs_cache/<item>/readalong.json`. Opening the EPUB matches it by file name, then by title, so nothing has to be copied to the reader.
- To get the file into the folder:
  - `make_readalong_sidecar.py ... --abs-folder "<item folder via SMB/NFS>" --abs-scan`, or
  - `publish_sidecar.py` afterwards.
  - `--abs-scan` asks ABS to rescan that one item (`POST /api/items/<id>/scan`, admin token). Without it, the folder watcher or the next library scan picks the file up.
  - ABS's own upload can only create new items, so copying into the folder is the way.
- Grimmory/Booklore alternative: `--grimmory-book-id N` attaches the file to the book as a *supplementary* file (`POST /api/v1/books/N/files?isBook=false`). It's kept and backed up with the book. The plugin doesn't fetch from Grimmory, so download it next to the EPUB from there.
- Simplest of all: put `Book.readalong.json` next to the EPUB on the reader. The plugin looks there first.

## Build and install the plugin
```
python plugin/build_plugin.py --test
```
Copy `build/audiobook.koplugin` into KOReader's `plugins` folder:
- Kobo: `.adds/koreader/plugins/`
- Kindle: `koreader/plugins/`
- Android: `koreader/plugins/`

Restart KOReader. Don't use the plugin's **Check for updates**: it reinstalls upstream and removes these changes. Setup for ABS and Cloudflare Access is in `plugin/overlay/CF-ACCESS-PATCH.md`.

## On the G14 (once)
Windows (native). In `sidecar/`:
```
py -3.12 -m venv %USERPROFILE%\readalong-venv
%USERPROFILE%\readalong-venv\Scripts\activate
pip install -r requirements.txt nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
winget install Gyan.FFmpeg
```
If the CUDA DLLs aren't found, add the venv's `Lib\site-packages\nvidia\cublas\bin` and `...\cudnn\bin` to PATH.

Or use Docker Desktop with WSL2: `cd sidecar && docker compose build readalong-gpu`.

## Per book
1. Fetch from ABS. On the LAN, Cloudflare isn't needed:
   ```
   set ABS_URL=http://<abs-lan-address>:13378
   set ABS_TOKEN=<your ABS API token>
   python abs_fetch.py "Book title" -o books
   ```
   - It prints the item id (`li_…`) and creates `books/<Title>/<Title>.epub` and `audio/`.
   - `abs_item.json` records the item's folder on the server.
   - If ABS has no EPUB for the item, copy in the EPUB you read in KOReader. It must be the identical file.
2. Align, then store with the audiobook:
   ```
   python make_readalong_sidecar.py "books/<Title>/<Title>.epub" "books/<Title>/audio" --abs-item-id li_xxx --model medium.en ^
       --abs-folder "\\server\audiobooks\Author\Title" --abs-scan
   ```
   - With Docker: `docker compose run --rm readalong-gpu "/books/<Title>/<Title>.epub" "/books/<Title>/audio" --abs-item-id li_xxx --model medium.en`.
   - `matched_in_span` in the printed stats should be above 0.9. Lower means the wrong edition or abridged audio.
   - `python validate_sidecar.py` gives an independent check.
3. On the reader:
   - Go to Audiobookshelf, browse, and **Download** the item. The message says "Read-along timings included".
   - Open the EPUB, go to Audiobook, then Play. It offers the ABS position ("Jump there?"), highlights sentences, and syncs progress back to ABS every 60 s and on pause.
   - Settings: **Highlight words** gives a moving word box. Turn on **Group short cues** if e-ink flashes.
   - To seek the audio, select text and choose **Play aligned from here**.

## Automatic: the companion service
Rather than running the steps above per book, run `companion/` on the always-on XPS:
- It scans ABS every few hours and queues each book that has audio and an EPUB but no `*.readalong.json`.
- It generates the sidecar, validates it, and drops it into the item folder.
- It keeps state and retries in SQLite and shows everything at `http://<xps-host>:8765`.
- It can hand the heavy work to the G14 (`WORKER_MODE=both` plus `docker-compose.worker.yml`).

Setup is in [`companion/README.md`](companion/README.md).

## Timing (per hour of audio)
| machine | model | estimate |
|---|---|---|
| 8-core Xeon VM (measured) | small.en int8 | 4.4 min (RTF 0.073) |
| Dell XPS 13 dual-core (estimate) | small.en | ~10–20 min |
| ASUS G14 RTX 4080 (estimate, not measured) | small.en / medium.en / large-v3-turbo fp16 | ~0.5–1 / ~1–2 / ~1–2 min, so a 10 h book takes ~10–20 min |

Prototype accuracy (Gift of the Magi, 13 min, small.en on CPU):
- 98.5% of narrated words matched, and every word has a time.
- 53 of 60 spot-checked words are within ±0.25 s.
- All 165 sentence xpointers land on the right word.

## Tests
- `python plugin/build_plugin.py --test`: Lua, sidecar parsing, highlight ranges, ABS fetch and matching.
- `python sidecar/tests/test_publish_sidecar.py`: ABS copy, scan and Grimmory upload against a fake server.
- `python -m unittest discover -s companion/tests -v`: the companion service, against a fake ABS and a fake transcriber.

## Licence and attribution
- `plugin/` is derived from [stradichenko/audiobook.koplugin](https://github.com/stradichenko/audiobook.koplugin), tag `v0.6.13` (commit `9a59606baddc67d31ed801fe20c942c425aed396`), © its authors, licensed under the **GNU AGPL-3.0**.
- The modifications and the rest of this repository are released under the same licence. See `LICENSE` and `NOTICE.md`.
