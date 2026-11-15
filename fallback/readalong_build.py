#!/usr/bin/env python3
"""Build an EPUB3 read-along (Media Overlays) from an EPUB + audiobook.

Pipeline: faster-whisper word timestamps -> sequence-align transcript to the
book text -> wrap words (or sentences) in <span id> -> SMIL per chapter ->
re-zip EPUB with the audio embedded.  Prototype; for a whole library use
Storyteller (see ../README.md).

usage: readalong_build.py BOOK.epub TRANSCRIPT.json AUDIO OUT.epub
       [--granularity word|sentence] [--audio-name NAME]
TRANSCRIPT.json: {"words":[{"w":..,"s":..,"e":..}, ...]} for a single audio
file (multi-file books: concatenate with offsets, or let Storyteller do it).
"""
import argparse, difflib, json, os, re, shutil, sys, tempfile, zipfile, posixpath
from lxml import etree

XHTML = "http://www.w3.org/1999/xhtml"
OPF = "http://www.idpf.org/2007/opf"
SKIP_TAGS = {"script", "style", "head", "title", "svg", "math", "rt", "rp"}
TOKEN_RE = re.compile(r"\S+")
SENT_END = re.compile(r"[.!?…][\"'”’)\]]*$")


def norm_parts(tok):
    # "eighty-seven" -> ["eighty","seven"]; "sterling—something" -> 2 words
    out = []
    for p in re.split(r"[-–—/]+", tok.lower()):
        p = p.replace("’", "'")
        p = re.sub(r"[^a-z0-9']", "", p).strip("'")
        if p:
            out.append(p)
    return out


def ftime(t):
    h, rem = divmod(max(t, 0.0), 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%06.3f" % (h, m, s)


class Doc:
    def __init__(self, href, path):
        self.href, self.path = href, path
        parser = etree.XMLParser(remove_blank_text=False, resolve_entities=False)
        self.tree = etree.parse(path, parser)
        self.tokens = []  # dicts: id, text, parts, span(element)

    def wrap_words(self, prefix):
        body = self.tree.getroot().find("{%s}body" % XHTML)
        if body is None:
            return
        n = [0]

        def make_spans(text):
            pieces, pos = [], 0
            for m in TOKEN_RE.finditer(text):
                pieces.append(("ws", text[pos:m.start()]))
                pieces.append(("tok", m.group()))
                pos = m.end()
            pieces.append(("ws", text[pos:]))
            return pieces

        def process(el):
            tag = etree.QName(el).localname if isinstance(el.tag, str) else None
            if tag is None or tag in SKIP_TAGS:
                return
            # recurse first over a static copy of children
            children = list(el)
            for c in children:
                process(c)
            # el.text
            if el.text and el.text.strip():
                self._splice(el, None, make_spans(el.text), prefix, n)
            for c in children:
                if c.tail and c.tail.strip():
                    self._splice(el, c, make_spans(c.tail), prefix, n)

        process(body)

    def _splice(self, parent, after, pieces, prefix, n):
        spans, lead = [], ""
        cur_tail_holder = None
        new_nodes = []
        for kind, s in pieces:
            if kind == "ws":
                if new_nodes:
                    new_nodes[-1].tail = (new_nodes[-1].tail or "") + s
                else:
                    lead += s
            else:
                n[0] += 1
                sp = etree.Element("{%s}span" % XHTML)
                sp.set("id", "%s-w%d" % (prefix, n[0]))
                sp.text = s
                new_nodes.append(sp)
                tok = {"id": sp.get("id"), "text": s, "parts": norm_parts(s), "el": sp}
                self.tokens.append(tok)
        if after is None:
            parent.text = lead
            for i, nd in enumerate(new_nodes):
                parent.insert(i, nd)
        else:
            after.tail = lead
            idx = list(parent).index(after)
            for i, nd in enumerate(new_nodes):
                parent.insert(idx + 1 + i, nd)

    def save(self):
        self.tree.write(self.path, xml_declaration=True, encoding="utf-8")


def align(tokens, words):
    """tokens: book tokens (parts lists); words: whisper words. Sets s/e on tokens."""
    a, a_owner = [], []
    for ti, t in enumerate(tokens):
        for p in t["parts"]:
            a.append(p)
            a_owner.append(ti)
    b, b_idx = [], []
    for wi, w in enumerate(words):
        for p in norm_parts(w["w"]):
            b.append(p)
            b_idx.append(wi)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    matched = 0
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            ti = a_owner[blk.a + k]
            w = words[b_idx[blk.b + k]]
            t = tokens[ti]
            t["s"] = min(t.get("s", w["s"]), w["s"])
            t["e"] = max(t.get("e", w["e"]), w["e"])
            t["matched"] = True
            matched += 1
    return matched, len(a), len(b)


def fill_gaps(tokens, max_gap_words=12):
    """Interpolate short unmatched runs between matched neighbours."""
    i, n = 0, len(tokens)
    while i < n:
        if tokens[i].get("matched") or not tokens[i]["parts"] and "s" in tokens[i]:
            i += 1
            continue
        j = i
        while j < n and not tokens[j].get("matched"):
            j += 1
        if i > 0 and j < n and (j - i) <= max_gap_words:
            t0, t1 = tokens[i - 1]["e"], tokens[j]["s"]
            if t1 > t0 and t1 - t0 < 15:
                lens = [max(len(tokens[k]["text"]), 1) for k in range(i, j)]
                tot, acc = float(sum(lens)), 0.0
                for k, L in zip(range(i, j), lens):
                    tokens[k]["s"] = t0 + (t1 - t0) * acc / tot
                    acc += L
                    tokens[k]["e"] = t0 + (t1 - t0) * acc / tot
                    tokens[k]["interp"] = True
        i = j


def refine_gaps(tokens, audio, model_name, min_run=3, log=print):
    """Whisper sometimes skips a stretch of narration. Re-transcribe just the
    audio between the matched neighbours of each unmatched run and align it."""
    import subprocess, numpy as np
    from faster_whisper import WhisperModel
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", audio, "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    arr = np.frombuffer(pcm, dtype=np.float32)
    model = WhisperModel(model_name, device="auto", compute_type="int8")
    i, n, fixed = 0, len(tokens), 0
    while i < n:
        if tokens[i].get("matched"):
            i += 1; continue
        j = i
        while j < n and not tokens[j].get("matched"):
            j += 1
        if i > 0 and j < n and (j - i) >= min_run:
            t0, t1 = max(tokens[i - 1]["e"] - 0.1, 0), tokens[j]["s"] + 0.1
            if 0.8 < t1 - t0 < 120:
                prompt = " ".join(t["text"] for t in tokens[max(0, i - 30):i])
                segs, _ = model.transcribe(arr[int(t0 * 16000):int(t1 * 16000)], language="en", word_timestamps=True,
                                           beam_size=5, initial_prompt=prompt, condition_on_previous_text=False)
                ws = [{"w": w.word, "s": w.start + t0, "e": w.end + t0} for s_ in segs for w in s_.words]
                before = sum(1 for t in tokens[i:j] if t.get("matched"))
                align(tokens[i:j], ws)
                got = sum(1 for t in tokens[i:j] if t.get("matched")) - before
                fixed += got
                log("refine %.1f-%.1fs: run of %d words, recovered %d" % (t0, t1, j - i, got))
        i = j
    return fixed


def to_sentences(doc):
    """Group a doc's timed word tokens into sentence cues (per parent element)."""
    cues, cur = [], []
    for t in doc.tokens:
        if "s" not in t:
            if cur:
                cues.append(cur); cur = []
            continue
        if cur and t["el"].getparent() is not cur[-1]["el"].getparent():
            cues.append(cur); cur = []
        cur.append(t)
        if SENT_END.search(t["text"]):
            cues.append(cur); cur = []
    if cur:
        cues.append(cur)
    return cues


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("epub"); ap.add_argument("transcript"); ap.add_argument("audio"); ap.add_argument("out")
    ap.add_argument("--granularity", choices=["word", "sentence"], default="word")
    ap.add_argument("--refine-model", default=None, help="faster-whisper model to re-transcribe skipped stretches (e.g. small.en)")
    ap.add_argument("--min-match", type=float, default=0.3, help="min fraction of a doc's words matched to narrate it")
    args = ap.parse_args()

    words = json.load(open(args.transcript))["words"]
    work = tempfile.mkdtemp(prefix="readalong-")
    with zipfile.ZipFile(args.epub) as z:
        z.extractall(work)
    cont = etree.parse(os.path.join(work, "META-INF/container.xml"))
    opf_rel = cont.find(".//{*}rootfile").get("full-path")
    opf_path = os.path.join(work, opf_rel)
    opf_dir = posixpath.dirname(opf_rel)
    opf = etree.parse(opf_path)
    root = opf.getroot()
    root.set("version", "3.0")
    manifest = root.find("{%s}manifest" % OPF)
    items = {it.get("id"): it for it in manifest.findall("{%s}item" % OPF)}
    spine = [ir.get("idref") for ir in root.find("{%s}spine" % OPF).findall("{%s}itemref" % OPF)]

    docs = []
    for idref in spine:
        it = items.get(idref)
        if it is None or it.get("media-type") != "application/xhtml+xml" or "nav" in (it.get("properties") or "") or "svg" in (it.get("properties") or ""):
            continue
        d = Doc(it.get("href"), os.path.join(work, opf_dir, it.get("href")))
        d.item = it
        d.wrap_words("d%d" % len(docs))
        docs.append(d)

    all_tokens = [t for d in docs for t in d.tokens]
    matched, na, nb = align(all_tokens, words)
    refined = refine_gaps(all_tokens, args.audio, args.refine_model) if args.refine_model else 0
    matched += refined
    fill_gaps(all_tokens)

    audio_name = "Audio/" + re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(args.audio))
    os.makedirs(os.path.join(work, opf_dir, "Audio"), exist_ok=True)
    shutil.copy(args.audio, os.path.join(work, opf_dir, audio_name))
    os.makedirs(os.path.join(work, opf_dir, "MediaOverlays"), exist_ok=True)
    meta = root.find("{%s}metadata" % OPF)
    meta.set("{http://www.w3.org/2000/xmlns/}media", "http://www.idpf.org/epub/vocab/overlays/#") if False else None
    root.set("prefix", (root.get("prefix", "") + " media: http://www.idpf.org/epub/vocab/overlays/#").strip())

    stats, total_dur, all_cues = [], 0.0, []
    for di, d in enumerate(docs):
        nwords = sum(1 for t in d.tokens if t["parts"])
        nmatch = sum(1 for t in d.tokens if t.get("matched"))
        frac = nmatch / nwords if nwords else 0
        keep = frac >= args.min_match and nmatch >= 5
        stats.append((d.href, nwords, nmatch, round(frac, 3), keep))
        # unwrap spans without timing (keeps markup minimal)
        cues = []
        if keep:
            if args.granularity == "word":
                cues = [[t] for t in d.tokens if "s" in t]
            else:
                sent = to_sentences(d)
                for si, grp in enumerate(sent):
                    # wrap the run of word spans into one sentence span
                    first = grp[0]["el"]; parent = first.getparent()
                    sspan = etree.Element("{%s}span" % XHTML); sspan.set("id", "%s-s%d" % (first.get("id").split("-")[0], si))
                    parent.insert(list(parent).index(first), sspan)
                    for t in grp:
                        tail = t["el"].tail
                        sspan.append(t["el"])
                    # move the last tail out so following text stays outside
                    sspan.tail = grp[-1]["el"].tail; grp[-1]["el"].tail = None
                    cues.append([{"id": sspan.get("id"), "s": grp[0]["s"], "e": grp[-1]["e"], "text": " ".join(t["text"] for t in grp)}])
        # strip ids/spans that carry no cue: unwrap untimed word spans
        cue_ids = {c[0]["id"] for c in cues}
        for t in d.tokens:
            el = t["el"]
            if args.granularity == "word" and t["id"] in cue_ids:
                continue
            if args.granularity == "sentence" or not keep or t["id"] not in cue_ids:
                # unwrap: merge text into previous sibling/parent
                parent = el.getparent(); prev = el.getprevious()
                txt = (el.text or "") + (el.tail or "")
                if prev is not None:
                    prev.tail = (prev.tail or "") + txt
                else:
                    parent.text = (parent.text or "") + txt
                parent.remove(el)
        d.save()
        if not cues:
            continue
        # monotonic, non-overlapping clips
        flat = [c[0] for c in cues]
        for k in range(len(flat)):
            if k and flat[k]["s"] < flat[k - 1]["e"]:
                flat[k]["s"] = flat[k - 1]["e"]
            if flat[k]["e"] < flat[k]["s"]:
                flat[k]["e"] = flat[k]["s"]
        for k in range(len(flat)):  # no zero-length cues: borrow from the gap after
            if flat[k]["e"] - flat[k]["s"] < 0.08:
                nxt = flat[k + 1]["s"] if k + 1 < len(flat) else flat[k]["s"] + 0.15
                flat[k]["e"] = max(flat[k]["e"], min(flat[k]["s"] + 0.15, nxt))
        smil_name = "MediaOverlays/%s.smil" % os.path.splitext(posixpath.basename(d.href))[0]
        rel_text = posixpath.relpath(d.href, "MediaOverlays")
        rel_audio = posixpath.relpath(audio_name, "MediaOverlays")
        lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<smil xmlns="http://www.w3.org/ns/SMIL" xmlns:epub="http://www.idpf.org/2007/ops" version="3.0">',
                 '<body>', '<seq id="seq-%d" epub:textref="%s">' % (di, rel_text)]
        for k, c in enumerate(flat):
            lines.append('<par id="p%d-%d"><text src="%s#%s"/><audio src="%s" clipBegin="%s" clipEnd="%s"/></par>'
                         % (di, k, rel_text, c["id"], rel_audio, ftime(c["s"]), ftime(c["e"])))
        lines += ['</seq>', '</body>', '</smil>']
        open(os.path.join(work, opf_dir, smil_name), "w").write("\n".join(lines) + "\n")
        sid = "smil-%d" % di
        el = etree.SubElement(manifest, "{%s}item" % OPF, id=sid, href=smil_name, **{"media-type": "application/smil+xml"})
        d.item.set("media-overlay", sid)
        dur = flat[-1]["e"] - flat[0]["s"]
        total_dur = max(total_dur, flat[-1]["e"])
        m = etree.SubElement(meta, "{%s}meta" % OPF, property="media:duration", refines="#" + sid); m.text = ftime(dur)
        all_cues.append((d.href, flat))

    etree.SubElement(manifest, "{%s}item" % OPF, id="audio-0", href=audio_name, **{"media-type": "audio/mpeg"})
    m = etree.SubElement(meta, "{%s}meta" % OPF, property="media:duration"); m.text = ftime(total_dur)
    m = etree.SubElement(meta, "{%s}meta" % OPF, property="media:active-class"); m.text = "-epub-media-overlay-active"
    opf.write(opf_path, xml_declaration=True, encoding="utf-8")

    # zip: mimetype first, stored
    if os.path.exists(args.out):
        os.remove(args.out)
    with zipfile.ZipFile(args.out, "w") as z:
        z.write(os.path.join(work, "mimetype"), "mimetype", compress_type=zipfile.ZIP_STORED)
        for dp, _, fns in os.walk(work):
            for fn in sorted(fns):
                full = os.path.join(dp, fn); rel = os.path.relpath(full, work)
                if rel == "mimetype":
                    continue
                ct = zipfile.ZIP_STORED if rel.endswith((".mp3", ".m4a", ".m4b", ".mp4")) else zipfile.ZIP_DEFLATED
                z.write(full, rel, compress_type=ct)
    report = {"granularity": args.granularity, "book_words": na, "asr_words": nb, "matched_words": matched,
              "docs": stats, "cues": sum(len(f) for _, f in all_cues),
              "interpolated": sum(1 for t in all_tokens if t.get("interp"))}
    json.dump({"report": report, "cues": [{"doc": h, "id": c["id"], "s": round(c["s"], 3), "e": round(c["e"], 3),
               "text": c.get("text") or next((t["text"] for t in all_tokens if t["id"] == c["id"]), "")}
               for h, f in all_cues for c in f]}, open(args.out + ".cues.json", "w"), indent=0)
    print(json.dumps(report, indent=1))
    shutil.rmtree(work)


if __name__ == "__main__":
    main()
