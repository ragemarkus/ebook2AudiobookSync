# Read-along sidecar (unmodified EPUB + Audiobookshelf audiobook)

Word/sentence read-along for books that have **no** Media Overlays. The EPUB
stays untouched; the timing lives in a sidecar file next to it, generated
off-device by forced alignment (`make_readalong_sidecar.py`). Audio is the
book's Audiobookshelf item, downloaded through the plugin's ABS browser, so
listening position syncs back to ABS.

```
Books/
  The Gift of the Magi.epub              <- the normal EPUB
  The Gift of the Magi.readalong.json    <- this sidecar
```

The plugin looks for `<book>.readalong.json`, then `<book>.epub.readalong.json`.
When one exists it is used instead of the EPUB's own SMIL; the EPUB's SMIL
remains the fallback if the sidecar cannot be used (for example, the
audiobook is not downloaded yet).

## Format v1

UTF-8 JSON, one object:

| key | meaning |
|---|---|
| `format` | `"koreader-readalong"` |
| `version` | `1` |
| `generator`, `created` | informational |
| `granularity` | `"sentence+word"`: one cue per sentence, word timings inside |
| `book.title`, `book.file`, `book.sha1` | identifies the EPUB the cues were made for |
| `book.spine` | content-document basenames in spine order; position N (1-based) is crengine's `DocFragment[N]` |
| `audio.abs_item_id` | Audiobookshelf library item id (`li_...`) whose position is synced; may be `""` |
| `audio.duration` | total seconds |
| `audio.tracks[]` | `{index, file, duration, offset}` in ABS `media.audioFiles` order; `offset` = global start second of the track |
| `audio.audio_dir` | optional: folder (relative to the sidecar) holding the track files for offline use without ABS |
| `stats` | alignment statistics from the generator |
| `cue_fields` | names of the cue array columns (v1: below) |
| `cues[]` | arrays, ordered by time and by reading order |

Cue columns (`cue_fields`):

| # | name | type | meaning |
|---|---|---|---|
| 1 | `t0` | number | global start, seconds (ABS book timeline) |
| 2 | `t1` | number | global end |
| 3 | `spine` | int | 1-based spine index of the content document |
| 4 | `xp` | string | crengine xpointer of the cue's first character, e.g. `/body/DocFragment[3]/body/div[1]/p[19]/text().426` (element path with 1-based same-name sibling indices; `text()[k]` counts non-whitespace text nodes; offset is in whitespace-collapsed text) |
| 5 | `bxp` | string | xpointer of the enclosing block element (`p`, `div`, `li`, `h1`…) |
| 6 | `bw` | int | 0-based index of the cue's first word among the block's whitespace-separated words |
| 7 | `text` | string | the sentence as printed (used to verify the range and as the text-match fallback) |
| 8 | `words` | array | `[[start_ms, dur_ms, "word"], ...]`, start relative to `t0`; may be empty |

Global time to track: the track with the largest `offset <= t0`; local time = `t - offset`.

## How the plugin uses it (`readalongsidecar.lua`)

* Resolves audio: track files next to the sidecar (or in `audio_dir`) first,
  else the plugin's ABS download of `audio.abs_item_id` (the file count must
  match `tracks`).
* Turns cues into the same timing entries the SMIL parser produces (ids
  `ra:<n>`, track-local times, `words`), so MediaSync overlay mode does the
  rest: sentence highlight, optional moving word highlight (**Highlight words**),
  e-ink cue grouping, page follow, chapter list, "Play aligned from here".
* Start xpointer per cue: tries `xp` (nudged up to 3 characters for whitespace
  differences), verifies the cue's opening words are really there, else walks
  the words of `bxp` and takes the occurrence closest to `bw`. If both fail,
  the existing on-page text matching is used.
* Position sync: while a sidecar book plays, progress goes to
  `audio.abs_item_id` as global seconds (track offset + local position) on the
  usual 60 s / pause / stop schedule. Starting from the menu offers the ABS
  position first ("Jump there?"), so listening on the phone moves the book.
* Tap text → audio: select text and use **Play aligned from here**; the cue
  is matched by block path then sentence text.

## Where the plugin looks for it

1. `Book.readalong.json` (or `Book.epub.readalong.json`) next to the EPUB.
2. A sidecar stored **with the audiobook on Audiobookshelf**: any file named
   `*.readalong.json` in the ABS item's folder. ABS lists it among the item's
   library files (type "metadata"; ABS only parses `metadata.json`, so it is
   left alone). When the plugin downloads that item from the ABS browser it
   also fetches the sidecar to `abs_cache/<item id>/readalong.json` and writes
   `readalong.meta` (item id, EPUB file name, title, sha1). Opening an EPUB
   then matches it by EPUB file name, else by title, and the sidecar's
   `abs_item_id` defaults to that item. Needs the ABS user's download
   permission (same as the audio).

Getting it onto the server: `make_readalong_sidecar.py --abs-folder <item
folder> [--abs-scan]` or `publish_sidecar.py` (copies into the folder, asks ABS
to rescan the item with an admin token). Grimmory/Booklore can hold it as a
supplementary file (`--grimmory-book-id`), but the plugin does not fetch from
Grimmory; copy it next to the EPUB from there.

## Generating one

See `make_readalong_sidecar.py` (faster-whisper word timestamps, CUDA when
available; windowed fuzzy alignment to the EPUB words; re-transcription of
stretches Whisper skipped; interpolation of short gaps). Validate with
`validate_sidecar.py`.
