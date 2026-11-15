#!/usr/bin/env python3
"""Generate a KOReader read-along sidecar (book.readalong.json) for an
UNMODIFIED EPUB and its (Audiobookshelf) audiobook.

    python make_readalong_sidecar.py BOOK.epub AUDIO [AUDIO ...] \
        [--abs-item-id ID] [--model small.en] [--device auto|cuda|cpu] [-o OUT]
        [--abs-folder ITEM_FOLDER] [--abs-scan] [--grimmory-book-id N]

--abs-folder/--abs-scan/--grimmory-book-id store the result with the book on
the server (see publish_sidecar.py); the KOReader plugin then fetches it when
it downloads the Audiobookshelf item.

AUDIO: the audiobook's track files in ABS order (a directory is expanded and
natural-sorted).  Times in the sidecar are global book seconds (track offset
+ local time), the same timeline ABS uses for progress.

Pipeline: faster-whisper word timestamps per track (CUDA float16 when a GPU
is visible, else CPU int8) -> windowed fuzzy alignment of the transcript to
the EPUB's words -> re-transcribe stretches Whisper skipped -> interpolate
short unmatched runs -> sentence cues with per-word timings and crengine
xpointers.  See SIDECAR_SPEC.md.
"""
import argparse, bisect, datetime, difflib, hashlib, json, os, posixpath, re, subprocess, sys, time, zipfile
from lxml import etree

VERSION = "0.1.0"
XHTML = "http://www.w3.org/1999/xhtml"
BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "td", "th", "dd", "dt",
         "pre", "figcaption", "section", "article", "aside", "header", "footer", "body"}
SKIP = {"script", "style", "head", "title", "svg", "math", "rt", "rp", "noscript"}
SENT_END = re.compile(r"[.!?…]+[\"'”’)\]]*$")
AUDIO_EXT = (".mp3", ".m4a", ".m4b", ".mp4", ".ogg", ".opus", ".flac", ".wav", ".aac")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def num_words(n):
    """Integer -> spoken words (so ASR "21" matches book "Twenty-one")."""
    if n < 20:
        return [ONES[n]]
    if n < 100:
        return [TENS[n // 10]] + ([ONES[n % 10]] if n % 10 else [])
    if n < 1000:
        return [ONES[n // 100], "hundred"] + (num_words(n % 100) if n % 100 else [])
    if n < 1000000:
        return num_words(n // 1000) + ["thousand"] + (num_words(n % 1000) if n % 1000 else [])
    return [str(n)]


def norm_parts(tok):
    out = []
    for p in re.split(r"[-–—/]+", tok.lower().replace("’", "'")):
        p = re.sub(r"[^a-z0-9'.,]", "", p).strip("'.,")
        if re.fullmatch(r"\d{1,3}(,\d{3})+|\d+", p):
            out += num_words(int(p.replace(",", "")))
            continue
        if re.fullmatch(r"\d+\.\d+", p):  # $1.87 -> one eighty seven-ish: keep digits split
            a_, b_ = p.split(".")
            out += num_words(int(a_)) + num_words(int(b_))
            continue
        p = re.sub(r"[^a-z0-9']", "", p).strip("'")
        if p:
            out.append(p)
    return out


def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


# ----------------------------------------------------------------- EPUB words
class Book:
    def __init__(self, path):
        self.path = path
        self.z = zipfile.ZipFile(path)
        cont = etree.fromstring(self.z.read("META-INF/container.xml"))
        opf_rel = cont.find(".//{*}rootfile").get("full-path")
        self.opf_dir = posixpath.dirname(opf_rel)
        opf = etree.fromstring(self.z.read(opf_rel))
        items = {i.get("id"): i for i in opf.iter("{*}item")}
        self.title = (opf.findtext(".//{*}title") or "").strip()
        # DocFragment[N] in crengine == 1-based position in the spine (all itemrefs)
        self.spine = []
        for ir in opf.iter("{*}itemref"):
            it = items.get(ir.get("idref"))
            self.spine.append(it.get("href") if it is not None else "")
        self.tokens = []   # dict(text, parts, spine, xp, bxp, bw)
        for si, href in enumerate(self.spine, start=1):
            if not href or not href.lower().endswith((".xhtml", ".html", ".htm", ".xml")):
                continue
            it = next((i for i in items.values() if i.get("href") == href), None)
            if it is not None and "nav" in (it.get("properties") or ""):
                continue
            try:
                root = etree.fromstring(self.z.read(posixpath.join(self.opf_dir, href)),
                                        etree.XMLParser(recover=True, resolve_entities=False))
            except Exception as e:  # noqa
                log("skip", href, e)
                continue
            body = next((e for e in root.iter() if isinstance(e.tag, str) and etree.QName(e).localname == "body"), None)
            if body is not None:
                self._walk_doc(si, href, body)

    @staticmethod
    def _local(el):
        return etree.QName(el).localname if isinstance(el.tag, str) else None

    def _path(self, el, body):
        segs = []
        while el is not None and el is not body:
            par = el.getparent()
            name = self._local(el)
            idx = 1 + sum(1 for s in el.itersiblings(preceding=True) if self._local(s) == name)
            segs.append("%s[%d]" % (name, idx))
            el = par
        return "body" + "".join("/" + s for s in reversed(segs))

    def _walk_doc(self, si, href, body):
        prefix = "/body/DocFragment[%d]/" % si
        block_words = {}

        def text_nodes(el):
            # (owner, kind, text, text_index) in document order; text() index
            # counts only non-whitespace text children of `owner` (crengine
            # drops whitespace-only nodes)
            idx = 0
            if el.text and el.text.strip():
                idx += 1
                yield el, el.text, idx
            for c in el:
                if self._local(c) in SKIP or not isinstance(c.tag, str):
                    pass
                else:
                    for x in text_nodes(c):
                        yield x
                if c.tail and c.tail.strip():
                    idx += 1
                    yield el, c.tail, idx

        for owner, text, tidx in text_nodes(body):
            blk = owner
            while blk is not None and self._local(blk) not in BLOCK:
                blk = blk.getparent()
            blk = blk if blk is not None else body
            bpath = self._path(blk, body)
            opath = self._path(owner, body)
            collapsed = re.sub(r"\s+", " ", text)
            for m in re.finditer(r"\S+", collapsed):
                bw = block_words.get(bpath, 0)
                block_words[bpath] = bw + 1
                tsel = "text()" if tidx == 1 else "text()[%d]" % tidx
                self.tokens.append({
                    "text": m.group(), "parts": norm_parts(m.group()), "spine": si, "href": href,
                    "xp": "%s%s/%s.%d" % (prefix, opath, tsel, m.start()),
                    "bxp": prefix + bpath, "bw": bw,
                })


# ------------------------------------------------------------- transcription
def ffprobe_duration(p):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def decode(p, start=None, dur=None):
    import numpy as np
    cmd = ["ffmpeg", "-v", "error"]
    if start is not None:
        cmd += ["-ss", "%.3f" % start]
    cmd += ["-i", p]
    if dur is not None:
        cmd += ["-t", "%.3f" % dur]
    cmd += ["-ac", "1", "-ar", "16000", "-f", "f32le", "-"]
    return np.frombuffer(subprocess.run(cmd, capture_output=True, check=True).stdout, dtype=np.float32)


def load_model(name, device):
    from faster_whisper import WhisperModel
    if device == "auto":
        try:
            import ctranslate2
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:  # noqa
            device = "cpu"
    ct = "float16" if device == "cuda" else "int8"
    log("whisper: %s on %s (%s)" % (name, device, ct))
    return WhisperModel(name, device=device, compute_type=ct), device


def transcribe(model, audio, offset, language, prompt=None):
    segs, _ = model.transcribe(audio, language=language, word_timestamps=True, vad_filter=True,
                               beam_size=5, condition_on_previous_text=False, initial_prompt=prompt)
    return [{"w": w.word, "s": w.start + offset, "e": w.end + offset} for s in segs for w in s.words]


# ------------------------------------------------------------------ alignment
def match_into(tokens, lo, hi, words):
    """Fuzzy-match ASR `words` into tokens[lo:hi]; sets s/e/matched. Returns
    index (exclusive) of the last matched token or None."""
    a, own = [], []
    for ti in range(lo, hi):
        for p in tokens[ti]["parts"]:
            a.append(p); own.append(ti)
    b, bidx = [], []
    for wi, w in enumerate(words):
        for p in norm_parts(w["w"]):
            b.append(p); bidx.append(wi)
    if not a or not b:
        return None
    last = None
    for blk in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        if blk.size < 2 and blk.size != 0 and len(a) > 50:
            continue  # lone 1-word matches in a big window are mostly noise
        for k in range(blk.size):
            t = tokens[own[blk.a + k]]; w = words[bidx[blk.b + k]]
            if t.get("matched") and t["s"] <= w["s"]:
                t["e"] = max(t["e"], w["e"]); continue
            t["s"] = min(t.get("s", w["s"]), w["s"]); t["e"] = max(t.get("e", w["e"]), w["e"])
            t["matched"] = True
            last = own[blk.a + k] + 1
    return last


def align_windowed(tokens, words, win=1200, ahead=6000, first_ahead=40000):
    """Walk the transcript in windows, matching each into a sliding book window
    that starts at the last confident match (narration is in book order)."""
    ptr, i, n = 0, 0, len(tokens)
    while i < len(words):
        chunk = words[i:i + win]
        lo = max(0, ptr - 300)
        hi = min(n, ptr + (first_ahead if ptr == 0 else ahead))
        last = match_into(tokens, lo, hi, chunk)
        if last:
            ptr = max(ptr, last)
        i += win


def drop_outliers(tokens):
    """Matched tokens must be time-monotonic in book order; unmatch the few that
    are not (spurious matches against repeated phrases)."""
    idx = [i for i, t in enumerate(tokens) if t.get("matched")]
    if not idx:
        return
    # longest non-decreasing subsequence on start times
    starts = [tokens[i]["s"] for i in idx]
    tails, tails_i, prev = [], [], [-1] * len(idx)
    for k, s in enumerate(starts):
        j = bisect.bisect_right(tails, s)
        if j == len(tails):
            tails.append(s); tails_i.append(k)
        else:
            tails[j] = s; tails_i[j] = k
        prev[k] = tails_i[j - 1] if j > 0 else -1
    keep, k = set(), tails_i[-1]
    while k >= 0:
        keep.add(idx[k]); k = prev[k]
    dropped = 0
    for i in idx:
        if i not in keep:
            tokens[i].pop("matched", None); tokens[i].pop("s", None); tokens[i].pop("e", None); dropped += 1
    return dropped


def runs(tokens):
    i, n = 0, len(tokens)
    while i < n:
        if tokens[i].get("matched"):
            i += 1; continue
        j = i
        while j < n and not tokens[j].get("matched"):
            j += 1
        yield i, j
        i = j


def refine(tokens, model, tracks, language, min_run=3):
    fixed = 0
    for i, j in list(runs(tokens)):
        if i == 0 or j >= len(tokens) or j - i < min_run:
            continue
        g0, g1 = tokens[i - 1]["e"] - 0.1, tokens[j]["s"] + 0.1
        if not (0.8 < g1 - g0 < 180):
            continue
        tr = track_at(tracks, g0)
        if tr is None or g1 > tr["offset"] + tr["duration"] + 0.5:
            continue  # spans a track boundary: leave to interpolation
        arr = decode(tr["path"], g0 - tr["offset"], g1 - g0)
        prompt = " ".join(t["text"] for t in tokens[max(0, i - 30):i])
        ws = transcribe(model, arr, g0, language, prompt)
        before = sum(1 for t in tokens[i:j] if t.get("matched"))
        sub = tokens[i:j]
        match_into(sub, 0, len(sub), ws)
        got = sum(1 for t in sub if t.get("matched")) - before
        fixed += got
        log("refine %.1f-%.1fs: %d unmatched words, recovered %d" % (g0, g1, j - i, got))
    return fixed


def interpolate(tokens, max_words=25, max_gap=20.0):
    n = 0
    for i, j in list(runs(tokens)):
        if i == 0 or j >= len(tokens) or j - i > max_words:
            continue
        t0, t1 = tokens[i - 1]["e"], tokens[j]["s"]
        if not (0 <= t1 - t0 < max_gap):
            continue
        lens = [max(len(tokens[k]["text"]), 1) for k in range(i, j)]
        tot, acc = float(sum(lens)), 0.0
        for k, L in zip(range(i, j), lens):
            tokens[k]["s"] = t0 + (t1 - t0) * acc / tot
            acc += L
            tokens[k]["e"] = t0 + (t1 - t0) * acc / tot
            tokens[k]["interp"] = True
            n += 1
    return n


def track_at(tracks, t):
    for tr in tracks:
        if tr["offset"] <= t < tr["offset"] + tr["duration"]:
            return tr
    return tracks[-1] if tracks and t >= tracks[-1]["offset"] else None


# ----------------------------------------------------------------------- cues
def build_cues(tokens, max_words=60):
    cues, cur = [], []

    def flush():
        timed = [t for t in cur if "s" in t]
        if timed and len(timed) >= max(1, len(cur) // 2):
            cues.append((cur[:], timed))
        cur.clear()

    for t in tokens:
        if cur and (t["bxp"] != cur[-1]["bxp"] or len(cur) >= max_words):
            flush()
        cur.append(t)
        if SENT_END.search(t["text"]):
            flush()
    flush()
    out, prev_end = [], 0.0
    for allt, timed in cues:
        t0 = max(timed[0]["s"], prev_end)
        t1 = max(timed[-1]["e"], t0 + 0.05)
        prev_end = t1
        words = []
        for t in allt:
            if "s" in t:
                ws = max(t["s"], t0); we = min(max(t["e"], ws + 0.04), t1)
                words.append([int(round((ws - t0) * 1000)), int(round((we - ws) * 1000)), t["text"]])
        first = allt[0]
        out.append([round(t0, 3), round(t1, 3), first["spine"], first["xp"], first["bxp"], first["bw"],
                    " ".join(t["text"] for t in allt), words])
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("epub")
    ap.add_argument("audio", nargs="+", help="audio files in ABS track order, or a directory")
    ap.add_argument("-o", "--out", help="default: BOOK.readalong.json next to the EPUB")
    ap.add_argument("--abs-item-id", default="", help="Audiobookshelf library item id (for position sync)")
    ap.add_argument("--model", default="small.en", help="faster-whisper model (GPU: medium.en / large-v3-turbo)")
    ap.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    ap.add_argument("--language", default="en")
    ap.add_argument("--transcript-cache", help="directory for per-track transcript JSON (resume / reruns)")
    ap.add_argument("--no-refine", action="store_true")
    import publish_sidecar
    publish_sidecar.add_args(ap)
    a = ap.parse_args()

    files = []
    for p in a.audio:
        if os.path.isdir(p):
            files += sorted((os.path.join(p, f) for f in os.listdir(p) if f.lower().endswith(AUDIO_EXT)), key=natural_key)
        else:
            files.append(p)
    tracks, off = [], 0.0
    for i, p in enumerate(files, 1):
        d = ffprobe_duration(p)
        tracks.append({"index": i, "path": p, "file": os.path.basename(p), "duration": round(d, 3), "offset": round(off, 3)})
        off += d
    T0 = time.time()
    book = Book(a.epub)
    log("book: %d spine items, %d words; audio: %d tracks, %.1f h" % (len(book.spine), len(book.tokens), len(tracks), off / 3600))

    model, device = load_model(a.model, a.device)
    words, t_asr = [], time.time()
    for tr in tracks:
        cache = a.transcript_cache and os.path.join(a.transcript_cache, "%03d-%s.json" % (tr["index"], tr["file"]))
        if cache and os.path.exists(cache):
            ws = json.load(open(cache))
        else:
            ws = transcribe(model, decode(tr["path"]), tr["offset"], a.language)
            if cache:
                os.makedirs(a.transcript_cache, exist_ok=True); json.dump(ws, open(cache, "w"))
        log("track %d/%d: %d words" % (tr["index"], len(tracks), len(ws)))
        words += ws
    t_asr = time.time() - t_asr

    toks = book.tokens
    align_windowed(toks, words)
    dropped = drop_outliers(toks)
    refined = 0 if a.no_refine else refine(toks, model, tracks, a.language)
    drop_outliers(toks)
    interp = interpolate(toks)
    cues = build_cues(toks)

    narrated = [t for t in toks if t.get("matched")]
    first_i = next((i for i, t in enumerate(toks) if t.get("matched")), 0)
    last_i = max((i for i, t in enumerate(toks) if t.get("matched")), default=0)
    span = toks[first_i:last_i + 1]
    stats = {
        "book_words": len(toks), "asr_words": len(words), "matched_words": len(narrated),
        "narrated_span_words": len(span),
        "matched_in_span": round(sum(1 for t in span if t.get("matched")) / max(len(span), 1), 4),
        "timed_in_span": round(sum(1 for t in span if "s" in t) / max(len(span), 1), 4),
        "refined_words": refined, "interpolated_words": interp, "outliers_dropped": dropped or 0,
        "cues": len(cues), "asr_seconds": round(t_asr, 1), "total_seconds": round(time.time() - T0, 1),
        "device": device, "model": a.model,
    }
    h = hashlib.sha1(open(a.epub, "rb").read()).hexdigest()
    side = {
        "format": "koreader-readalong", "version": 1,
        "generator": "make_readalong_sidecar.py %s" % VERSION,
        "created": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "granularity": "sentence+word",
        "book": {"title": book.title, "file": os.path.basename(a.epub), "sha1": h, "spine": [posixpath.basename(s) for s in book.spine]},
        "audio": {"abs_item_id": a.abs_item_id, "duration": round(off, 3),
                  "tracks": [{k: tr[k] for k in ("index", "file", "duration", "offset")} for tr in tracks]},
        "stats": stats,
        "cue_fields": ["t0", "t1", "spine", "xp", "bxp", "bw", "text", "words"],
        "cues": cues,
    }
    out = a.out or re.sub(r"\.epub$", "", a.epub, flags=re.I) + ".readalong.json"
    with open(out, "w") as f:
        json.dump(side, f, ensure_ascii=False, separators=(",", ":"))
    log(json.dumps(stats, indent=1))
    log("wrote", out, "(%.0f KB)" % (os.path.getsize(out) / 1024))
    if a.abs_folder or a.abs_scan or a.grimmory_book_id:
        publish_sidecar.publish(out, a.abs_folder, a.abs_item_id, a.abs_scan, a.grimmory_book_id)


if __name__ == "__main__":
    main()
