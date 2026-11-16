#!/usr/bin/env python3
"""Stand-in for make_readalong_sidecar.py in tests (no Whisper).  Writes a
valid sidecar for the fixture EPUB (tests/fakes.py:make_epub).
FAKE_GEN_MODE: ok (default) | fail | lowmatch | badxp | slow
FAKE_GEN_LOG: append the argv as one JSON line."""
import argparse, json, os, sys, time

ap = argparse.ArgumentParser()
ap.add_argument("epub")
ap.add_argument("audio", nargs="+")
ap.add_argument("-o", "--out")
ap.add_argument("--abs-item-id", default="")
a, _ = ap.parse_known_args()
mode = os.environ.get("FAKE_GEN_MODE", "ok")
if os.environ.get("FAKE_GEN_LOG"):
    with open(os.environ["FAKE_GEN_LOG"], "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\n")
if mode == "fail":
    print("Traceback: simulated generator crash", file=sys.stderr)
    sys.exit(3)
if mode == "slow":
    time.sleep(float(os.environ.get("FAKE_GEN_SLEEP", "3")))
for p in a.audio:
    assert os.path.getsize(p) > 0, p
off = 0.0
tracks = []
for i, p in enumerate(a.audio, 1):
    tracks.append({"index": i, "file": os.path.basename(p), "duration": 5.0, "offset": off})
    off += 5.0
xp2 = "/body/DocFragment[1]/body/p[1]/text().13" if mode != "badxp" else "/body/DocFragment[1]/body/p[1]/text().2"
cues = [[0.0, 1.2, 1, "/body/DocFragment[1]/body/p[1]/text().0", "", 0, "Hello world.",
         [[0, 500, "Hello"], [600, 500, "world."]]],
        [1.5, 3.0, 1, xp2, "", 2, "This is a test.",
         [[0, 300, "This"], [350, 200, "is"], [600, 200, "a"], [900, 500, "test."]]]]
side = {"format": "koreader-readalong", "version": 1, "generator": "fake",
        "book": {"title": "Fixture", "file": os.path.basename(a.epub), "sha1": "x", "spine": ["ch1.xhtml"]},
        "audio": {"abs_item_id": a.abs_item_id, "duration": off, "tracks": tracks},
        "stats": {"matched_in_span": 0.42 if mode == "lowmatch" else 0.985, "cues": 2},
        "cue_fields": ["t0", "t1", "spine", "xp", "bxp", "bw", "text", "words"], "cues": cues}
with open(a.out, "w") as f:
    json.dump(side, f)
