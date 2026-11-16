-- readalongsidecar.lua: parsing, timing data, time lookup, position mapping,
-- audio resolution and the Audiobookshelf auto-fetch / matching.
local H = dofile((debug.getinfo(1, "S").source:match("^@(.*[/\\])") or "./") .. "spec_helper.lua")
local Sidecar = H.load_overlay("readalongsidecar.lua")

describe("readalong sidecar: JSON decoding", function()
    it("decodes the subset the generator writes", function()
        local j = Sidecar.decodeJson('{"a":[1,2.5,-3e2,"x\\u2019y\\n\\"q\\""],"b":{"c":true,"d":null,"f":false},"e":[]}')
        assert.are.equal(1, j.a[1])
        assert.are.equal(2.5, j.a[2])
        assert.are.equal(-300, j.a[3])
        assert.are.equal("x\226\128\153y\n\"q\"", j.a[4])
        assert.is_true(j.b.c)
        assert.is_false(j.b.f)
        assert.is_nil(j.b.d)
        assert.are.equal(0, #j.e)
    end)

    it("round-trips a full sample sidecar", function()
        local sc = Sidecar.decodeJson(H.json(H.sample_sidecar()))
        assert.are.equal("koreader-readalong", sc.format)
        assert.are.equal(4, #sc.cues)
        assert.are.equal("Hello there world.", sc.cues[1][7])
        assert.are.equal(900, sc.cues[1][8][3][1])
    end)

    it("errors on malformed JSON", function()
        assert.is_false((pcall(Sidecar.decodeJson, '{"a":[1,2')))
    end)
end)

describe("readalong sidecar: read()", function()
    after_each(H.cleanup)

    local function write_sc(tbl_or_text)
        local d = H.tmpdir()
        local text = type(tbl_or_text) == "string" and tbl_or_text or H.json(tbl_or_text)
        return H.write(d .. "/Book.readalong.json", text)
    end

    it("accepts a valid sidecar", function()
        local sc, err = Sidecar.read(write_sc(H.sample_sidecar()))
        assert.is_nil(err)
        assert.are.equal("li_123", sc.audio.abs_item_id)
    end)

    it("rejects a missing file, non-JSON, wrong format, wrong version and empty tracks", function()
        local _, e1 = Sidecar.read("/nonexistent/x.readalong.json")
        assert.matches("cannot open", e1)
        local _, e2 = Sidecar.read(write_sc("<html>Access denied</html>"))
        assert.matches("not valid JSON", e2)
        local s = H.sample_sidecar(); s.format = "something-else"
        local _, e3 = Sidecar.read(write_sc(s))
        assert.matches("not a readalong sidecar", e3)
        s = H.sample_sidecar(); s.version = 2
        local _, e4 = Sidecar.read(write_sc(s))
        assert.matches("unsupported sidecar version 2", e4)
        s = H.sample_sidecar(); s.audio.tracks = {}
        local _, e5 = Sidecar.read(write_sc(s))
        assert.matches("no cues or tracks", e5)
    end)
end)

describe("readalong sidecar: buildTimingData", function()
    local p, td
    before_each(function()
        p = Sidecar:new()
        td = p:buildTimingData(H.sample_sidecar(), { "/a/01.mp3", "/a/02.mp3" })
    end)

    it("emits one entry per cue with stable fragment ids", function()
        assert.are.equal(4, #td)
        for i, e in ipairs(td) do
            assert.are.equal("ra:" .. i, e.fragment_id)
            assert.is_true(Sidecar.isSidecarFragment(e.fragment_id))
            assert.is_true(e.sidecar)
        end
        assert.is_false(Sidecar.isSidecarFragment("p0001"))
        assert.is_false(Sidecar.isSidecarFragment(nil))
    end)

    it("maps global times to track-local times on the right track", function()
        assert.are.equal("/a/01.mp3", td[1].audio_path)
        assert.is_true(H.near(td[1].start_time, 10.0))
        assert.are.equal("/a/02.mp3", td[4].audio_path)
        assert.are.equal("02.mp3", td[4].audio_src)
        assert.is_true(H.near(td[4].start_time, 50.0))
        assert.is_true(H.near(td[4].end_time, 52.0))
        assert.is_true(H.near(td[4].global_start, 150.0))
    end)

    it("keeps a cue that crosses a track boundary on its starting track, clamped", function()
        assert.are.equal("/a/01.mp3", td[3].audio_path)
        assert.is_true(H.near(td[3].start_time, 98.0))
        assert.is_true(H.near(td[3].end_time, 100.0))
    end)

    it("converts word offsets (ms from cue start) into track-local seconds", function()
        local w = td[1].words
        assert.are.equal(3, #w)
        assert.is_true(H.near(w[2].start_time, 10.5))
        assert.is_true(H.near(w[2].end_time, 10.8))
        assert.is_true(H.near(w[3].end_time, 11.9))
        assert.are.equal("world.", w[3].text)
        assert.is_true(H.near(td[4].words[2].start_time, 50.6))
        assert.is_nil(td[3].words)       -- no words -> sentence-only cue
    end)

    it("fills spine document, xpointers and CRe-style element paths", function()
        assert.are.equal("ch1.xhtml", td[1].text_doc)
        assert.are.equal("ch2.xhtml", td[4].text_doc)
        assert.are.equal("body/div/p", td[1].elem_path)
        assert.are.equal("body/p[3]", td[4].elem_path)
        assert.are.equal("/body/DocFragment[2]/body/div[1]/p[1]/text().19", td[2].xpointer)
        assert.are.equal(3, td[2].block_word)
        assert.are.same({ "cover.xhtml", "ch1.xhtml", "ch2.xhtml" }, p._spine_hrefs)
    end)

    it("uses the v1 default field order when cue_fields is absent", function()
        local sc = H.sample_sidecar(); sc.cue_fields = nil
        local t2 = Sidecar:new():buildTimingData(sc, { "/a/01.mp3", "/a/02.mp3" })
        assert.are.equal("Second sentence.", t2[2].text)
        assert.is_true(H.near(t2[2].start_time, 13.0))
    end)

    it("records the ABS item id, or nil when the generator left it empty", function()
        assert.are.equal("li_123", p.abs_item_id)
        local sc = H.sample_sidecar(); sc.audio.abs_item_id = ""
        local q = Sidecar:new(); q:buildTimingData(sc, { "/a/01.mp3", "/a/02.mp3" })
        assert.is_nil(q.abs_item_id)
    end)
end)

describe("readalong sidecar: lookup by audio time and position mapping", function()
    local p
    before_each(function()
        p = Sidecar:new()
        p:buildTimingData(H.sample_sidecar(), { "/a/01.mp3", "/a/02.mp3" })
    end)

    it("finds the sentence at a global (ABS) book time", function()
        assert.are.equal("ra:1", p:entryAtGlobalTime(10.0).fragment_id)
        assert.are.equal("ra:1", p:entryAtGlobalTime(12.9).fragment_id)  -- gap before cue 2 keeps cue 1
        assert.are.equal("ra:2", p:entryAtGlobalTime(14.0).fragment_id)
        assert.are.equal("ra:3", p:entryAtGlobalTime(120.0).fragment_id)
        assert.are.equal("ra:4", p:entryAtGlobalTime(151.0).fragment_id)
        assert.are.equal("ra:4", p:entryAtGlobalTime(9999).fragment_id)  -- past the end -> last
        assert.are.equal("ra:1", p:entryAtGlobalTime(0).fragment_id)     -- before the first -> first
        assert.is_nil(p:entryAtGlobalTime(nil))
        assert.is_nil(Sidecar:new():entryAtGlobalTime(5))
    end)

    it("maps a track-local position back to global book seconds (ABS progress)", function()
        assert.are.equal(7, p:globalPosition("/a/01.mp3", 7))
        assert.are.equal(107, p:globalPosition("/a/02.mp3", 7))
        assert.are.equal(100, p:globalPosition("/a/02.mp3"))
        assert.is_nil(p:globalPosition("/a/unknown.mp3", 7))
    end)

    it("global -> local -> global is the identity for every cue start", function()
        for _, e in ipairs(p._timing) do
            assert.is_true(H.near(p:globalPosition(e.audio_path, e.start_time), e.global_start))
        end
    end)
end)

describe("readalong sidecar: audio resolution", function()
    after_each(H.cleanup)

    it("prefers track files next to the sidecar", function()
        local d = H.tmpdir()
        H.write(d .. "/01.mp3", "x"); H.write(d .. "/02.mp3", "x")
        local paths, how = Sidecar:new():_resolveAudio(H.sample_sidecar(), d .. "/Book.readalong.json")
        assert.are.equal("local", how)
        assert.are.same({ d .. "/01.mp3", d .. "/02.mp3" }, paths)
    end)

    it("honours audio_dir (relative and absolute)", function()
        local d = H.tmpdir()
        H.mkdir(d .. "/tracks")
        H.write(d .. "/tracks/01.mp3", "x"); H.write(d .. "/tracks/02.mp3", "x")
        local sc = H.sample_sidecar(); sc.audio.audio_dir = "tracks"
        local paths = Sidecar:new():_resolveAudio(sc, d .. "/Book.readalong.json")
        assert.are.equal(d .. "/tracks/02.mp3", paths[2])
        sc.audio.audio_dir = d .. "/tracks"
        paths = Sidecar:new():_resolveAudio(sc, "/elsewhere/Book.readalong.json")
        assert.are.equal(d .. "/tracks/01.mp3", paths[1])
    end)

    it("falls back to the ABS download of the item, in ABS order", function()
        local d = H.tmpdir()
        H.write(d .. "/c1.mp3", "x"); H.write(d .. "/c2.mp3", "x")
        local cache = { getAudioPaths = function(_, id)
            if id == "li_123" then return { d .. "/c1.mp3", d .. "/c2.mp3" } end
        end }
        local paths, how = Sidecar:new{ abs_cache = cache }:_resolveAudio(H.sample_sidecar(), d .. "/B.readalong.json")
        assert.are.equal("abs", how)
        assert.are.same({ d .. "/c1.mp3", d .. "/c2.mp3" }, paths)
    end)

    it("explains why audio can't be found", function()
        local d = H.tmpdir()
        H.write(d .. "/c1.mp3", "x")
        local sc = H.sample_sidecar()
        local _, why = Sidecar:new{ abs_cache = { getAudioPaths = function() return { d .. "/c1.mp3" } end } }
            :_resolveAudio(sc, d .. "/B.readalong.json")
        assert.matches("has 1 audio files, sidecar expects 2", why)
        _, why = Sidecar:new{ abs_cache = { getAudioPaths = function() return nil end } }:_resolveAudio(sc, d .. "/B.readalong.json")
        assert.matches("download this audiobook", why)
        _, why = Sidecar:new{ abs_cache = { getAudioPaths = function() return { d .. "/c1.mp3", d .. "/gone.mp3" } end } }
            :_resolveAudio(sc, d .. "/B.readalong.json")
        assert.matches("downloaded audio file missing", why)
        sc.audio.abs_item_id = ""
        _, why = Sidecar:new():_resolveAudio(sc, d .. "/B.readalong.json")
        assert.matches("no Audiobookshelf item id", why)
    end)
end)

describe("readalong sidecar: auto-fetch from Audiobookshelf and matching", function()
    local root, cache_dir
    before_each(function()
        root = H.tmpdir()
        cache_dir = root .. "/abs_cache"
        H.mkdir(cache_dir); H.mkdir(root .. "/Books")
    end)
    after_each(H.cleanup)

    local function body(title, file, item)
        local sc = H.sample_sidecar()
        sc.book.title, sc.book.file, sc.audio.abs_item_id = title, file, item or ""
        return H.json(sc)
    end

    local function client(files, payload, opts)
        opts = opts or {}
        local c = { server_url = "https://abs.example", requested = {} }
        function c.getItemDetails(_, id) c.details_for = id return { id = id, libraryFiles = files } end
        function c.downloadFile(self, url, dest)
            self.requested[#self.requested + 1] = url
            if opts.fail then return false, "HTTP 403" end
            H.write(dest, payload)
            return true
        end
        return c
    end

    it("downloads the item's *.readalong.json via the library-file API and writes readalong.meta", function()
        local item_dir = cache_dir .. "/li_777"; H.mkdir(item_dir)
        local c = client({
            { ino = "11", metadata = { filename = "01 - Track.mp3" } },
            { ino = "42", metadata = { filename = "The Gift of the Magi.readalong.json" } },
            { ino = "12", metadata = { filename = "metadata.json" } },
        }, body("The Gift of the Magi", "The Gift of the Magi.epub"))
        local path = Sidecar.fetchFromAbs(c, { id = "li_777" }, item_dir)
        assert.are.equal(item_dir .. "/readalong.json", path)
        assert.are.same({ "https://abs.example/api/items/li_777/file/42/download" }, c.requested)
        assert.are.equal("li_777", c.details_for)
        local meta = H.read(item_dir .. "/readalong.meta")
        assert.matches("item_id=li_777\n", meta)
        assert.matches("file=The Gift of the Magi.epub\n", meta)
        assert.matches("source=The Gift of the Magi.readalong.json\n", meta)
        assert.is_false(H.exists(item_dir .. "/readalong.json.part"))
    end)

    it("uses libraryFiles already on the item and picks the first sidecar by name", function()
        local item_dir = cache_dir .. "/li_2"; H.mkdir(item_dir)
        local c = client({}, body("B", "B.epub"))
        local item = { id = "li_2", libraryFiles = {
            { ino = "9", metadata = { filename = "Zed.readalong.json" } },
            { ino = "8", metadata = { filename = "Alpha.READALONG.JSON" } },
        } }
        assert.is_not_nil(Sidecar.fetchFromAbs(c, item, item_dir))
        assert.is_nil(c.details_for)          -- no extra API call
        assert.matches("/file/8/download$", c.requested[1])
    end)

    it("reports items without a sidecar, failed downloads and non-sidecar responses", function()
        local item_dir = cache_dir .. "/li_3"; H.mkdir(item_dir)
        local p, why = Sidecar.fetchFromAbs(client({ { ino = "1", metadata = { filename = "a.mp3" } } }, ""), { id = "li_3" }, item_dir)
        assert.is_nil(p); assert.are.equal("none", why)
        local files = { { ino = "1", metadata = { filename = "B.readalong.json" } } }
        p, why = Sidecar.fetchFromAbs(client(files, "", { fail = true }), { id = "li_3" }, item_dir)
        assert.is_nil(p); assert.matches("download failed: HTTP 403", why)
        -- e.g. a Cloudflare Access login page instead of the file
        p, why = Sidecar.fetchFromAbs(client(files, "<html>Sign in</html>"), { id = "li_3" }, item_dir)
        assert.is_nil(p); assert.matches("not valid JSON", why)
        assert.is_false(H.exists(item_dir .. "/readalong.json"))
        assert.is_false(H.exists(item_dir .. "/readalong.json.part"))
        p, why = Sidecar.fetchFromAbs(nil, { id = "x" }, item_dir)
        assert.is_nil(p); assert.are.equal("bad arguments", why)
    end)

    it("replaces an older fetched sidecar", function()
        local item_dir = cache_dir .. "/li_4"; H.mkdir(item_dir)
        H.write(item_dir .. "/readalong.json", "old")
        local files = { { ino = "1", metadata = { filename = "B.readalong.json" } } }
        Sidecar.fetchFromAbs(client(files, body("New Title", "B.epub")), { id = "li_4" }, item_dir)
        assert.matches("New Title", H.read(item_dir .. "/readalong.json"))
    end)

    local function fetch(item_id, title, file)
        local item_dir = cache_dir .. "/" .. item_id; H.mkdir(item_dir)
        local files = { { ino = "1", metadata = { filename = "x.readalong.json" } } }
        return Sidecar.fetchFromAbs(client(files, body(title, file)), { id = item_id }, item_dir)
    end

    it("findSidecar: a sidecar next to the book wins (Book.readalong.json, then Book.epub.readalong.json)", function()
        fetch("li_5", "Book", "Book.epub")
        local d = root .. "/Books"
        H.write(d .. "/Book.epub.readalong.json", "{}")
        assert.are.equal(d .. "/Book.epub.readalong.json", Sidecar.findSidecar(d .. "/Book.epub", { abs_cache_dir = cache_dir }))
        H.write(d .. "/Book.readalong.json", "{}")
        assert.are.equal(d .. "/Book.readalong.json", Sidecar.findSidecar(d .. "/Book.epub", { abs_cache_dir = cache_dir }))
        assert.are.equal(d .. "/Book.readalong.json", Sidecar.findSidecar(d .. "/Book.EPUB"))
        assert.is_nil(Sidecar.findSidecar(d .. "/Other.epub"))
        assert.is_nil(Sidecar.findSidecar(""))
        assert.is_nil(Sidecar.findSidecar(nil))
    end)

    it("findSidecar: matches an ABS-fetched sidecar by EPUB file name (case/punctuation-insensitive), returning the item id", function()
        fetch("li_6", "The Gift of the Magi", "The Gift of the Magi.epub")
        local p, id = Sidecar.findSidecar(root .. "/Books/the_gift-of the MAGI.epub", { abs_cache_dir = cache_dir })
        assert.are.equal(cache_dir .. "/li_6/readalong.json", p)
        assert.are.equal("li_6", id)
    end)

    it("findSidecar: falls back to the title, and file-name matches beat title matches", function()
        fetch("li_7", "Shared Title", "edition-a.epub")
        local p, id = Sidecar.findSidecar(root .. "/Books/renamed.epub", { abs_cache_dir = cache_dir, title = "Shared Title" })
        assert.are.equal("li_7", id); assert.is_not_nil(p)
        fetch("li_8", "Different", "renamed.epub")
        local _, id2 = Sidecar.findSidecar(root .. "/Books/renamed.epub", { abs_cache_dir = cache_dir, title = "Shared Title" })
        assert.are.equal("li_8", id2)
        assert.is_nil(Sidecar.findSidecar(root .. "/Books/unrelated.epub", { abs_cache_dir = cache_dir, title = "Nope" }))
    end)

    it("findSidecar ignores cache folders without readalong.meta or readalong.json", function()
        H.mkdir(cache_dir .. "/li_9")
        H.write(cache_dir .. "/li_9/readalong.json", H.json(H.sample_sidecar()))   -- no meta
        H.mkdir(cache_dir .. "/li_10")
        H.write(cache_dir .. "/li_10/readalong.meta", "item_id=li_10\nfile=Sample.epub\n")  -- no json
        assert.is_nil(Sidecar.findSidecar(root .. "/Books/Sample.epub", { abs_cache_dir = cache_dir }))
    end)

    it("loadFromEpub: an ABS-fetched sidecar takes the item id from the ABS item", function()
        fetch("li_11", "Sample", "Sample.epub")       -- generator left abs_item_id empty
        local adir = cache_dir .. "/li_11/audio"; H.mkdir(adir)
        H.write(adir .. "/001-01.mp3", "x"); H.write(adir .. "/002-02.mp3", "x")
        local seen
        local p = Sidecar:new{ abs_cache = { getAudioPaths = function(_, id)
            seen = id
            return { adir .. "/001-01.mp3", adir .. "/002-02.mp3" }
        end } }
        local td, err = p:loadFromEpub(root .. "/Books/Sample.epub", root)
        assert.is_nil(err)
        assert.are.equal(4, #td)
        assert.are.equal("li_11", seen)
        assert.are.equal("li_11", p.abs_item_id)
        assert.are.equal("abs", p.audio_source)
        assert.are.equal("Sample", p.sidecar.title)
        local _, e2 = Sidecar:new():loadFromEpub(root .. "/Books/None.epub", root)
        assert.are.equal("no readalong sidecar", e2)
    end)
end)
