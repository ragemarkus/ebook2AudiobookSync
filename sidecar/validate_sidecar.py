#!/usr/bin/env python3
"""Validate a readalong sidecar: schema/timing sanity, xpointers against the
EPUB text, and an independent audio spot-check (re-recognise sampled word and
sentence-start clips with a different Whisper model)."""
import argparse, json, os, random, re, subprocess, sys
ap = argparse.ArgumentParser(); ap.add_argument("sidecar"); ap.add_argument("audio_dir")
ap.add_argument("--samples", type=int, default=40); ap.add_argument("--tol", type=float, default=0.25)
ap.add_argument("--model", default="base.en"); ap.add_argument("--seed", type=int, default=3)
a = ap.parse_args()
sc = json.load(open(a.sidecar))
assert sc["format"] == "koreader-readalong" and sc["version"] == 1
F = {k: i for i, k in enumerate(sc["cue_fields"])}
cues = sc["cues"]; tracks = sc["audio"]["tracks"]
errs, prev = [], 0
for i, c in enumerate(cues):
    t0, t1 = c[F["t0"]], c[F["t1"]]
    if t1 < t0: errs.append("neg %d" % i)
    if t0 < prev - 1e-6: errs.append("order %d" % i)
    prev = t0
    if not re.match(r"^/body/DocFragment\[\d+\]/body(/[a-z0-9]+\[\d+\])*/text\(\)(\[\d+\])?\.\d+$", c[F["xp"]]): errs.append("xp %d %s" % (i, c[F["xp"]]))
    for w in c[F["words"]]:
        if w[0] < 0 or w[0] + w[1] > (t1 - t0) * 1000 + 1: errs.append("word-range %d" % i)
durs = sorted(c[F["t1"]] - c[F["t0"]] for c in cues)
nw = sum(len(c[F["words"]]) for c in cues)
print(json.dumps({"cues": len(cues), "timed_words": nw, "errors": len(errs), "first_errors": errs[:5],
                  "cue_dur_median": round(durs[len(durs)//2], 2), "cue_dur_max": round(durs[-1], 2)}, indent=1))
# xpointer self-check: resolve every cue xp against the EPUB's XHTML the way
# crengine addresses it (element path, non-whitespace text() index, offset in
# whitespace-collapsed text) and confirm the cue's first word starts there.
epub = os.path.join(os.path.dirname(a.sidecar), sc["book"]["file"])
if os.path.exists(epub):
    import zipfile, posixpath
    from lxml import etree
    z = zipfile.ZipFile(epub)
    opf_rel = etree.fromstring(z.read("META-INF/container.xml")).find(".//{*}rootfile").get("full-path")
    opf = etree.fromstring(z.read(opf_rel)); items = {i.get("id"): i.get("href") for i in opf.iter("{*}item")}
    spine = [items[i.get("idref")] for i in opf.iter("{*}itemref")]
    docs, ok_xp = {}, 0
    for c in cues:
        m = re.match(r"^/body/DocFragment\[(\d+)\]/body((?:/[a-z0-9]+\[\d+\])*)/text\(\)(?:\[(\d+)\])?\.(\d+)$", c[F["xp"]])
        n, path, tix, off = int(m.group(1)), m.group(2), int(m.group(3) or 1), int(m.group(4))
        if n not in docs:
            docs[n] = etree.fromstring(z.read(posixpath.join(posixpath.dirname(opf_rel), spine[n - 1])), etree.XMLParser(recover=True))
        el = next(e for e in docs[n].iter() if isinstance(e.tag, str) and etree.QName(e).localname == "body")
        for seg in [s_ for s_ in path.split("/") if s_]:
            name, k = re.match(r"([a-z0-9]+)\[(\d+)\]", seg).groups()
            el = [ch for ch in el if isinstance(ch.tag, str) and etree.QName(ch).localname == name][int(k) - 1]
        texts = ([el.text] if el.text and el.text.strip() else []) + [ch.tail for ch in el if ch.tail and ch.tail.strip()]
        node = re.sub(r"\s+", " ", texts[tix - 1])
        ok_xp += node[off:].startswith(c[F["text"]].split()[0])
    print("XPOINTERS: %d/%d cue xpointers land on the cue's first word in the EPUB" % (ok_xp, len(cues)))
if a.samples <= 0: sys.exit(1 if errs else 0)
import numpy as np
from faster_whisper import WhisperModel
m = WhisperModel(a.model, device="cpu", compute_type="int8")
def clip(g0, g1):
    tr = max((t for t in tracks if t["offset"] <= g0), key=lambda t: t["offset"])
    out = subprocess.run(["ffmpeg", "-v", "error", "-ss", "%.3f" % (g0 - tr["offset"]), "-i", os.path.join(a.audio_dir, tr["file"]),
                          "-t", "%.3f" % (g1 - g0), "-ac", "1", "-ar", "16000", "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.float32)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_readalong_sidecar import norm_parts
def nrm(s): return [p.replace("'", "") for t in s.split() for p in norm_parts(t)]
random.seed(a.seed)
pool = [(c[F["t0"]] + w[0] / 1000, c[F["t0"]] + (w[0] + w[1]) / 1000, w[2]) for c in cues for w in c[F["words"]]
        if len(re.sub(r"\W", "", w[2])) >= 5]
ok = 0; errs_s = []
for s, e, word in random.sample(pool, a.samples):
    segs, _ = m.transcribe(clip(max(0, s - a.tol), e + a.tol), language="en", beam_size=5, without_timestamps=True, condition_on_previous_text=False)
    heard = " ".join(x.text for x in segs); g = nrm(heard)
    hit = any(w in g or any(w[:4] == h[:4] for h in g) for w in nrm(word)); ok += hit
    print("%-4s %7.2f-%7.2f %-18s heard: %s" % ("OK" if hit else "MISS", s, e, word[:18], heard.strip()[:50]))
print("WORDS: %d/%d sampled words heard inside their clip +/-%.2fs" % (ok, a.samples, a.tol))
ok2 = 0; n2 = min(15, len(cues))
for c in random.sample(cues, n2):
    first3 = " ".join(c[F["text"]].split()[:3])
    segs, _ = m.transcribe(clip(max(0, c[F["t0"]] - 0.3), c[F["t0"]] + 1.5), language="en", beam_size=5, without_timestamps=True, condition_on_previous_text=False)
    heard = " ".join(x.text for x in segs); want = nrm(first3)[:1]; g = nrm(heard)[:4]
    hit = bool(want) and any(want[0][:4] == h[:4] for h in g); ok2 += hit
    print("%-4s cue start %7.2f  %-28s heard: %s" % ("OK" if hit else "MISS", c[F["t0"]], first3[:28], heard.strip()[:40]))
print("SENTENCE STARTS: %d/%d cue first-words heard in [t0-0.3s, t0+1.5s]" % (ok2, n2))
