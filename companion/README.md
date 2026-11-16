# Read-along companion

A small always-on service for the Dell XPS. It runs as a Docker container and:
- watches Audiobookshelf;
- queues every book that has **audio and an EPUB but no `*.readalong.json`**;
- generates the sidecar with the existing `sidecar/make_readalong_sidecar.py`;
- checks it with `sidecar/validate_sidecar.py` plus a quality gate;
- copies it into the ABS item folder, the same way `publish_sidecar.py --abs-folder` does.

The KOReader plugin then picks it up on its next download of the item. The G14's GPU can optionally do the heavy work instead.

```
 ABS API ──scan──▶ companion (XPS) ──queue (SQLite)──▶ local CPU worker
                     │  ▲                                    │
                     │  └──── remote worker (G14, CUDA) ◀────┘ pull / heartbeat / upload
                     ▼
     <ABS item folder>/<Book>.readalong.json   +   GET /api/sidecar/<item id>
```

## How it works
- **Scan.** At start-up, every `SCAN_INTERVAL` (default 6 h), or when you press **Rescan**:
  - It lists the book libraries with `GET /api/libraries/<id>/items` (paged).
  - For new or changed items, it reads `GET /api/items/<id>?expanded=1`.
  - Eligible items have `media.audioFiles` plus an `.epub` `ebookFile`.
  - Items that already list a `*.readalong.json` are marked `existing`.
  - Unchanged items (same `updatedAt`) are not fetched again.
- **Inputs.** Tracks are taken in `media.audioFiles` order, which is the list and order the KOReader plugin downloads. They are named `001-<file>` and so on, as `abs_fetch.py` does.
  - If the library folder is mounted (`ABS_PATH_MAP`), files are symlinked from it and nothing is downloaded.
  - Otherwise they're downloaded through the ABS API, with the Cloudflare Access headers when set.
- **Generate.** It runs `make_readalong_sidecar.py BOOK.epub 001-… 002-… --abs-item-id ID --model M --device D --transcript-cache …`.
  - The transcript cache means a retried job doesn't re-transcribe tracks it already finished.
- **Validate.** A sidecar is only published if all of these pass:
  - it is `koreader-readalong` v1, has cues, and `audio.abs_item_id` is the right item;
  - `stats.matched_in_span` is at least `MIN_MATCH` (default 0.85). Lower usually means the wrong edition or abridged audio;
  - `validate_sidecar.py --samples 0` passes its schema and timing checks;
  - at least `MIN_XPOINTER` of the sentence xpointers land on the right word in the EPUB.
  - A failure here sets **needs_review** and the job isn't retried. Use **Regenerate** after fixing the book.
- **Publish.** It writes `<EPUB name>.readalong.json` into the item folder atomically, using `publish_sidecar.abs_copy`.
  - With `ABS_SCAN_AFTER_PUBLISH=true` and an admin token, it also calls `POST /api/items/<id>/scan`.
  - A copy is always kept in `data/sidecars/` and served at `/api/sidecar/<item id>`.
  - If the folder isn't mounted, the status is **generated**: the sidecar is only served by the companion, not written to ABS.
- **Queue.** SQLite (`data/state.db`).
  - Crashes and network errors are retried with backoff: `RETRY_BASE_SECONDS` × 2ⁿ, up to `MAX_ATTEMPTS`, then **failed**.
  - Running jobs hold a lease (`LEASE_SECONDS`), renewed by heartbeat. If a worker disappears, its job goes back to the queue.
  - A restart re-queues the XPS's own job without counting the attempt.

### Who generates: `WORKER_MODE`
| mode | XPS CPU | G14 worker |
|---|---|---|
| `local` (default) | does every job | disabled |
| `remote` | never generates; only queues, validates and publishes | does every job |
| `both` | takes jobs that have waited `LOCAL_CLAIM_DELAY` s (default 6 h) | gets first pick whenever it's on |
| `off` | scan and serve only | disabled |

The G14 worker needs only `COMPANION_URL` and `WORKER_TOKEN`. It fetches the audio and EPUB **through the companion** (`/api/worker/jobs/<id>/file/<ino>`, limited to the job it holds), so no ABS or Cloudflare credentials live on the laptop. Its upload is validated again on the XPS before anything is written.

### Web UI and API
The UI is at `http://<xps-host>:8765/`. It shows counts, the queue, errors, the last scan, recent events, and **Rescan** / **Regenerate** buttons.

| method | path | |
|---|---|---|
| GET | `/api/health` | liveness |
| GET | `/api/status` | counts, running jobs, last scan, non-secret config |
| GET | `/api/queue[?status=pending,failed]` | items |
| GET | `/api/items/<id>` | one item and its events |
| GET | `/api/events?limit=50` | recent events |
| GET | `/api/sidecar/<id>` | the sidecar JSON |
| POST | `/api/rescan` | scan now (`ADMIN_TOKEN`) |
| POST | `/api/items/<id>/regenerate` | redo it, overwriting an existing sidecar (`ADMIN_TOKEN`). Also works for an item the scan hasn't seen yet. |
| POST | `/api/worker/claim`, `/api/worker/jobs/<id>/{heartbeat,complete,fail}`, GET `…/file/<ino>` | remote workers (`WORKER_TOKEN`, `X-Worker-Name`) |

GET endpoints are open to whoever can reach the port, and it's meant for the LAN. **Don't route it through the Cloudflare tunnel.** If you ever do, put it behind Access.

## Setup on the XPS (Ubuntu + Docker)
1. Get the code:
   ```
   git clone https://github.com/ragemarkus/ebook2AudiobookSync.git && cd ebook2AudiobookSync/companion
   cp env.example .env && chmod 600 .env && mkdir -p data
   ```
2. Edit `.env`:
   - `ABS_URL=http://<abs-host>:13378`: the LAN address. If ABS runs in Docker on the XPS, it's the host's LAN address or the ABS container name on a shared network.
   - `ABS_TOKEN`: ABS → Settings → Users → you → API token. Use an admin user if you want `ABS_SCAN_AFTER_PUBLISH=true`.
   - `ABS_LIBRARY_HOST_PATH`: the host folder ABS has as its library.
   - `ABS_PATH_MAP`: `<path ABS shows for that folder>=/library`. The ABS path is the library folder in ABS → Settings → Libraries, for example `/audiobooks`.
   - `PUID`/`PGID`: the owner of the library files (`stat -c '%u %g' /path/to/audiobooks`).
   - `ADMIN_TOKEN`: random, for example `openssl rand -hex 32`.
   - Only for the G14: `WORKER_MODE=both` (or `remote`) and `WORKER_TOKEN=$(openssl rand -hex 32)`.
   - Leave `CF_ACCESS_*` empty on the LAN.
3. Start it: `docker compose up -d --build`, then `docker compose logs -f`.
   - The first job downloads the Whisper model into `data/models`.
   - Open `http://<xps-host>:8765`.
   - Optional dry run first: `docker compose run --rm readalong-companion scan`, which runs one scan and prints the summary.
4. On the reader, re-download the ABS item once its status is **done**. The plugin says "Read-along timings included".

If ABS is only reachable through Cloudflare (not on the XPS's LAN):
- set `ABS_URL=https://<abs-public-host>` and `CF_ACCESS_CLIENT_ID/SECRET` from a service token;
- give that token a **Service Auth** policy on the Audiobookshelf Access app.

## Optional: GPU worker on the G14
Docker Desktop (WSL2) with GPU support, or Linux with the NVIDIA Container Toolkit:
```
git clone https://github.com/ragemarkus/ebook2AudiobookSync.git && cd ebook2AudiobookSync/companion
cp worker.env.example worker.env      # COMPANION_URL=http://<xps-host>:8765, WORKER_TOKEN = the XPS's value
docker compose -f docker-compose.worker.yml up -d --build
```
Native Windows instead, using the venv from the main README's "On the G14":
```
cd ebook2AudiobookSync\companion
set COMPANION_URL=http://<xps-host>:8765
set WORKER_TOKEN=...
set WORKER_NAME=g14
set WHISPER_MODEL=medium.en
set WHISPER_DEVICE=cuda
set DATA_DIR=%USERPROFILE%\readalong-worker
%USERPROFILE%\readalong-venv\Scripts\python -m readalong_companion worker
```
- Stop the worker any time. A job it was holding goes back to the queue after `LEASE_SECONDS`.
- With `WORKER_MODE=both`, the XPS picks up jobs the G14 hasn't taken within `LOCAL_CLAIM_DELAY`.

## Rough cost
- From the main README, per hour of audio: XPS with small.en is roughly 10–20 min (an estimate), so a 10 h book takes hours.
- The G14 with medium.en is estimated at 1–2 min per audio hour.
- Measured on this repo's 13-min demo, through the companion on an 8-core VM:
  - `small.en`: 52 s ASR, 98.6% matched, 165/165 xpointers;
  - `base.en`: 23 s, 90.3% matched.

## Tests
```
python -m unittest discover -s companion/tests -v
```
- The tests use a fake ABS API, which requires the Cloudflare Access headers and a bearer token.
- A fake transcriber (`tests/fake_generator.py`) stands in for Whisper.
- The real `validate_sidecar.py` and `publish_sidecar.abs_copy` are used.
- Covered: scan and pagination, eligibility, mounted and download modes, publishing, the quality gate, retries and backoff, leases, regenerate, the web API and auth, and the remote worker end to end.
- Optional real run with faster-whisper:
  ```
  REAL_GENERATOR_PYTHON=/path/to/venv/bin/python REAL_DEMO_DIR=/dir/with/book.epub+audio.mp3 \
      REAL_MODEL=small.en python -m unittest companion.tests.test_companion.TestRealGenerator -v
  ```
