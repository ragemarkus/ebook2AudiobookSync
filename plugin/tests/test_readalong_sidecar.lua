#!/usr/bin/env luajit
-- Tests for readalongsidecar.lua (sidecar read-along for unmodified EPUBs).
-- Run from the repo root: luajit dev/test_readalong_sidecar.lua [DEMO_DIR]
-- DEMO_DIR (optional): folder with "The Gift of the Magi.epub", its
-- .readalong.json and magi.mp3 -- runs the end-to-end load as well.

local here = (arg and arg[0]):gsub("[^/]+$", "")
package.preload["logger"] = function()
    return setmetatable({}, { __index = function() return function() end end })
end
package.preload["audiobook_gettext"] = function() return function(s) return s end end
local Sidecar = dofile(here .. "../readalongsidecar.lua")
local Utils = dofile(here .. "../utils.lua")

local failures, checks = {}, 0
local function ok(c, label) checks = checks + 1 if not c then failures[#failures + 1] = label end end
local function eq(a, b, label) ok(a == b, string.format("%s: expected %s, got %s", label, tostring(b), tostring(a))) end
local function near(a, b, label) ok(a and math.abs(a - b) < 1e-6, string.format("%s: expected %s, got %s", label, tostring(b), tostring(a))) end

-- ── JSON fallback decoder ────────────────────────────────────────────────
local j = Sidecar.decodeJson('{"a":[1,2.5,-3e2,"x\\u2019y\\n"],"b":{"c":true,"d":null},"e":[]}')
eq(j.a[2], 2.5, "json float"); eq(j.a[3], -300, "json exp"); eq(j.a[4], "x\226\128\153y\n", "json unicode escape")
eq(j.b.c, true, "json bool"); eq(#j.e, 0, "json empty array")

-- ── buildTimingData: two tracks, global -> track-local ───────────────────
local sc = {
    format = "koreader-readalong", version = 1,
    book = { spine = { "cover.xhtml", "ch1.xhtml", "ch2.xhtml" } },
    audio = { abs_item_id = "li_123", duration = 200,
        tracks = { { index = 1, file = "01.mp3", duration = 100, offset = 0 },
                   { index = 2, file = "02.mp3", duration = 100, offset = 100 } } },
    cue_fields = { "t0", "t1", "spine", "xp", "bxp", "bw", "text", "words" },
    cues = {
        { 10.0, 12.5, 2, "/body/DocFragment[2]/body/div[1]/p[1]/text().0", "/body/DocFragment[2]/body/div[1]/p[1]", 0,
          "Hello there world.", { { 0, 400, "Hello" }, { 500, 300, "there" }, { 900, 1000, "world." } } },
        { 98.0, 101.0, 2, "/body/DocFragment[2]/body/div[1]/p[2]/text().0", "/body/DocFragment[2]/body/div[1]/p[2]", 0,
          "Across the boundary.", {} },
        { 150.0, 152.0, 3, "/body/DocFragment[3]/body/p[3]/text().5", "/body/DocFragment[3]/body/p[3]", 1,
          "Second track.", { { 0, 500, "Second" } } },
    },
}
local p = Sidecar:new()
local td = p:buildTimingData(sc, { "/a/01.mp3", "/a/02.mp3" })
eq(#td, 3, "entries")
eq(td[1].audio_path, "/a/01.mp3", "cue1 track"); near(td[1].start_time, 10.0, "cue1 local start")
eq(td[2].audio_path, "/a/01.mp3", "boundary cue stays on starting track")
near(td[2].end_time, 100.0, "boundary cue clamped to track end")
eq(td[3].audio_path, "/a/02.mp3", "cue3 track 2"); near(td[3].start_time, 50.0, "cue3 local start")
near(td[3].global_start, 150.0, "cue3 global start")
eq(td[1].fragment_id, "ra:1", "fragment id"); eq(td[3].text_doc, "ch2.xhtml", "text_doc from spine index")
eq(td[1].elem_path, "body/div/p", "elem_path normalised like CRe tap paths")
eq(td[3].elem_path, "body/p[3]", "elem_path keeps non-first index")
near(td[1].words[2].start_time, 10.5, "word local start"); near(td[1].words[3].end_time, 11.9, "word local end")
eq(td[2].words, nil, "empty words -> nil")
eq(p.abs_item_id, "li_123", "abs item id")
near(p:globalPosition("/a/02.mp3", 7), 107, "global position track 2")
eq(p:entryAtGlobalTime(99).fragment_id, "ra:2", "entryAtGlobalTime mid")
eq(p:entryAtGlobalTime(5).fragment_id, "ra:1", "entryAtGlobalTime before first")
eq(p:entryAtGlobalTime(500).fragment_id, "ra:3", "entryAtGlobalTime after last")
eq(p._spine_hrefs[2], "ch1.xhtml", "spine hrefs for DocFragment mapping")
ok(Sidecar.isSidecarFragment("ra:9") and not Sidecar.isSidecarFragment("id12-s0"), "isSidecarFragment")
-- the overlay machinery's e-ink cue grouping still works on these entries
local groups = Utils.buildCueGroups(td, 0.5)
ok(groups and #groups >= 1, "buildCueGroups accepts sidecar entries")

-- ── audio resolution: ABS cache stub ─────────────────────────────────────
local tmp = os.tmpname(); os.remove(tmp); os.execute("mkdir -p " .. tmp)
for _, f in ipairs({ "c1.mp3", "c2.mp3" }) do local h = io.open(tmp .. "/" .. f, "w") h:write("x") h:close() end
local stub = { getAudioPaths = function(_, id) if id == "li_123" then return { tmp .. "/c1.mp3", tmp .. "/c2.mp3" } end end }
local q = Sidecar:new{ abs_cache = stub }
local paths, how = q:_resolveAudio(sc, tmp .. "/Book.readalong.json", nil)
eq(how, "abs", "audio from ABS cache"); eq(paths and paths[2], tmp .. "/c2.mp3", "ABS track order kept")
local stub1 = { getAudioPaths = function() return { tmp .. "/c1.mp3" } end }
local r, why = Sidecar:new{ abs_cache = stub1 }:_resolveAudio(sc, tmp .. "/B.readalong.json", nil)
ok(r == nil and tostring(why):find("expects 2"), "track count mismatch is reported")
local r2, why2 = Sidecar:new{ abs_cache = { getAudioPaths = function() return nil end } }:_resolveAudio(sc, tmp .. "/B.readalong.json", nil)
ok(r2 == nil and tostring(why2):find("download this audiobook"), "not-downloaded is reported")
for _, f in ipairs({ "01.mp3", "02.mp3" }) do local h = io.open(tmp .. "/" .. f, "w") h:write("x") h:close() end
local paths3, how3 = Sidecar:new{ abs_cache = stub }:_resolveAudio(sc, tmp .. "/Book.readalong.json", nil)
eq(how3, "local", "local track files next to the sidecar win")
local h = io.open(tmp .. "/Book.readalong.json", "w") h:write("{}") h:close()
eq(Sidecar.findSidecar(tmp .. "/Book.epub"), tmp .. "/Book.readalong.json", "findSidecar stem")
eq(Sidecar.findSidecar(tmp .. "/Other.epub"), nil, "findSidecar none")
local bad, berr = Sidecar.read(tmp .. "/Book.readalong.json")
ok(bad == nil and berr, "read rejects non-sidecar JSON")
os.execute("rm -rf " .. tmp)

-- ── ABS storage: fetch "*.readalong.json" from the item, then find it ────
do
    local root = os.tmpname(); os.remove(root)
    local cache_dir = root .. "/abs_cache"
    local item_dir = cache_dir .. "/li_777"
    os.execute("mkdir -p " .. item_dir .. " " .. root .. "/Books")
    local body = '{"format":"koreader-readalong","version":1,"book":{"title":"The Gift of the Magi",' ..
        '"file":"The Gift of the Magi.epub","sha1":"abc","spine":["a.xhtml"]},' ..
        '"audio":{"abs_item_id":"","duration":10,"tracks":[{"index":1,"file":"x.mp3","duration":10,"offset":0}]},' ..
        '"cues":[[0,1,1,"/body/DocFragment[1]/body/p[1]/text().0","/body/DocFragment[1]/body/p[1]",0,"Hi.",[]]]}'
    local requested
    local client = {
        server_url = "https://abs.example",
        getItemDetails = function(_, id)
            return { id = id, libraryFiles = {
                { ino = "11", metadata = { filename = "01 - Track.mp3" } },
                { ino = "42", metadata = { filename = "The Gift of the Magi.readalong.json" } },
                { ino = "12", metadata = { filename = "metadata.json" } },
            } }
        end,
        downloadFile = function(_, url, dest)
            requested = url
            local f = io.open(dest, "wb") f:write(body) f:close()
            return true
        end,
    }
    local path = Sidecar.fetchFromAbs(client, { id = "li_777" }, item_dir)
    eq(path, item_dir .. "/readalong.json", "fetchFromAbs stores readalong.json")
    eq(requested, "https://abs.example/api/items/li_777/file/42/download", "fetchFromAbs uses the library-file download API")
    local p1, id1 = Sidecar.findSidecar(root .. "/Books/The Gift of the Magi.epub", { abs_cache_dir = cache_dir })
    eq(p1, item_dir .. "/readalong.json", "findSidecar matches ABS sidecar by EPUB file name")
    eq(id1, "li_777", "findSidecar returns the ABS item id")
    local p2 = Sidecar.findSidecar(root .. "/Books/gift_magi_v2.epub", { abs_cache_dir = cache_dir, title = "The Gift of the Magi" })
    eq(p2, item_dir .. "/readalong.json", "findSidecar matches ABS sidecar by title")
    eq(Sidecar.findSidecar(root .. "/Books/Other.epub", { abs_cache_dir = cache_dir, title = "Other" }), nil,
        "findSidecar ignores unrelated books")
    -- the item id comes from the ABS item when the generator left it empty
    os.execute("mkdir -p " .. item_dir .. "/audio")
    local f = io.open(item_dir .. "/audio/001-x.mp3", "w") f:write("x") f:close()
    local lp = Sidecar:new{ abs_cache = { getAudioPaths = function(_, id) if id == "li_777" then return { item_dir .. "/audio/001-x.mp3" } end end } }
    local ltd, lerr = lp:loadFromEpub(root .. "/Books/The Gift of the Magi.epub", root)
    ok(ltd ~= nil, "load ABS-fetched sidecar: " .. tostring(lerr))
    eq(lp.abs_item_id, "li_777", "ABS item id filled from the fetched item")
    eq(lp.audio_source, "abs", "audio from the ABS download")
    -- an item without a sidecar
    local none_client = { server_url = "x", getItemDetails = function() return { libraryFiles = {} } end }
    local np, nerr = Sidecar.fetchFromAbs(none_client, { id = "li_1" }, item_dir)
    ok(np == nil and nerr == "none", "fetchFromAbs: item without sidecar")
    -- invalid JSON is rejected and not left behind
    local bad_client = { server_url = "x",
        getItemDetails = function() return { libraryFiles = { { ino = "1", metadata = { filename = "B.readalong.json" } } } } end,
        downloadFile = function(_, _, dest) local h = io.open(dest, "wb") h:write("<html>") h:close() return true end }
    local bdir = cache_dir .. "/li_bad"; os.execute("mkdir -p " .. bdir)
    local bp = Sidecar.fetchFromAbs(bad_client, { id = "li_bad" }, bdir)
    ok(bp == nil and io.open(bdir .. "/readalong.json") == nil, "fetchFromAbs rejects a non-sidecar response")
    os.execute("rm -rf " .. root)
end

-- ── resolveStartXPointer against a fake crengine document ────────────────
local FakeDoc = dofile(here .. "fake_crengine_doc.lua")
local base = "/body/DocFragment[3]/body/div"
local doc = FakeDoc({
    "One dollar and eighty-seven cents. That was all.",
    "The cat sat. The cat sat again. The dog ran.",
}, base)
local function entry(xp, bxp, bw, text) return { xpointer = xp, block_xpointer = bxp, block_word = bw, text = text } end
eq(Sidecar.resolveStartXPointer(doc, entry(base .. "/p[1]/text().35", base .. "/p[1]", 5, "That was all.")),
    base .. "/p[1]/text().35", "exact xpointer")
-- crengine trimmed one leading char: generator offset is one too far
eq(Sidecar.resolveStartXPointer(doc, entry(base .. "/p[1]/text().36", base .. "/p[1]", 5, "That was all.")),
    base .. "/p[1]/text().35", "xpointer nudged by whitespace difference")
-- unusable xpointer: block walk picks the occurrence nearest the word index
eq(Sidecar.resolveStartXPointer(doc, entry(base .. "/p[9]/text().0", base .. "/p[2]", 3, "The cat sat again.")),
    base .. "/p[2]/text().13", "block walk: second 'The cat sat' (bw=3)")
-- the block's own start xpointer is a valid start for the first word
local first = Sidecar.resolveStartXPointer(doc, entry(nil, base .. "/p[2]", 0, "The cat sat."))
ok(first == base .. "/p[2]" or first == base .. "/p[2]/text().0", "block walk: first occurrence (bw=0)")
eq(Sidecar.resolveStartXPointer(doc, entry(nil, base .. "/p[2]", 6, "The dog ran.")),
    base .. "/p[2]/text().32", "block walk: later sentence")
eq(Sidecar.resolveStartXPointer(doc, entry(nil, base .. "/p[2]", 0, "Nothing like this")), nil,
    "no match -> nil (caller falls back to text matching)")
-- the walk must not leave p[1] for p[10]
local many = {}
for i = 1, 10 do many[i] = "Para " .. i .. " text." end
many[10] = "Target words here."
local doc10 = FakeDoc(many, base)
eq(Sidecar.resolveStartXPointer(doc10, entry(nil, base .. "/p[1]", 0, "Target words here.")), nil,
    "block walk stays inside its block")
eq(Sidecar.resolveStartXPointer(doc10, entry(nil, base .. "/p[10]", 0, "Target words here.")) ~= nil, true,
    "block walk finds p[10]")
-- resolveFragment caches per id
local rp = Sidecar:new()
rp._by_frag["ra:1"] = entry(base .. "/p[1]/text().0", base .. "/p[1]", 0, "One dollar and")
eq(rp:resolveFragment(doc, "ra:1"), base .. "/p[1]/text().0", "resolveFragment")
eq(rp._xp_cache["ra:1"], base .. "/p[1]/text().0", "resolveFragment caches")

-- ── end to end on the prototype book (optional) ──────────────────────────
local demo = arg and arg[1]
if demo then
    local epub = demo .. "/The Gift of the Magi.epub"
    local sp = Sidecar:new()
    local tdd, err = sp:loadFromEpub(epub, here .. "..")
    ok(tdd ~= nil, "demo load: " .. tostring(err))
    if tdd then
        eq(sp.audio_source, "local", "demo audio next to the book")
        local mono, words, with_xp = true, 0, 0
        for i = 2, #tdd do if tdd[i].start_time < tdd[i - 1].start_time then mono = false end end
        for _, e in ipairs(tdd) do
            words = words + (e.words and #e.words or 0)
            if e.xpointer and e.xpointer:match("^/body/DocFragment%[3%]") then with_xp = with_xp + 1 end
        end
        ok(mono, "demo cues monotonic")
        print(string.format("demo: %d cues, %d timed words, %d xpointers in DocFragment[3], first=%q @%.2fs",
            #tdd, words, with_xp, tdd[1].text:sub(1, 40), tdd[1].start_time))
    end
end

if #failures > 0 then
    print(string.format("FAIL %d/%d", #failures, checks))
    for _, f in ipairs(failures) do print("  " .. f) end
    os.exit(1)
end
print(string.format("ok %d checks", checks))
