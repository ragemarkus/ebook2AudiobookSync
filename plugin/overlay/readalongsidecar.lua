--[[--
Read-along sidecar: word/sentence timings for an UNMODIFIED EPUB, stored next
to it as `<book>.readalong.json` and generated off-device (see
docs/READALONG_SIDECAR.md).  Audio comes from the book's Audiobookshelf item
(downloaded through the plugin's ABS browser) or from track files placed next
to the sidecar.  Times in the sidecar are global book seconds; entries are
emitted per audio track with track-local times so the existing SMIL overlay
playback (MediaSync overlay mode) drives highlighting, page follow and
"play from here" unchanged.

Duck-types the parts of EpubMediaOverlay that main.lua uses:
loadFromEpub(), _spine_hrefs, _chapter_titles, getCacheDir().

@module koplugin.audiobook.readalongsidecar
--]]

local ok_logger, logger = pcall(require, "logger")
if not ok_logger then
    logger = setmetatable({}, { __index = function() return function() end end })
end

local Sidecar = {
    FORMAT = "koreader-readalong",
    VERSION = 1,
    ID_PREFIX = "ra:",
}

function Sidecar:new(o)
    o = o or {}
    setmetatable(o, self)
    self.__index = self
    o.is_sidecar = true
    o._spine_hrefs = {}
    o._chapter_titles = {}
    o._by_frag = {}
    o._xp_cache = {}
    return o
end

-- ---------------------------------------------------------------------------
-- Locating and decoding
-- ---------------------------------------------------------------------------

local function file_exists(p)
    local f = p and io.open(p, "rb")
    if f then f:close() return true end
    return false
end

local function norm_key(s)
    return (tostring(s or ""):lower():gsub("%.epub$", ""):gsub("[^%w\128-\255]", ""))
end

local function read_meta(path)
    local f = io.open(path, "rb")
    if not f then return nil end
    local m = {}
    for line in f:lines() do
        local k, v = line:match("^([%w_]+)=(.*)$")
        if k then m[k] = v end
    end
    f:close()
    return m
end

local function list_dirs(root)
    local out = {}
    local ok, lfs = pcall(require, "libs/libkoreader-lfs")
    if ok and lfs and lfs.dir then
        local ok2 = pcall(function()
            for name in lfs.dir(root) do
                if name ~= "." and name ~= ".." then out[#out + 1] = name end
            end
        end)
        if ok2 then return out end
        out = {}
    end
    local h = io.popen('ls -1 "' .. root:gsub('"', '\\"') .. '" 2>/dev/null')
    if h then
        for name in h:lines() do out[#out + 1] = name end
        h:close()
    end
    return out
end

--- Sidecar path for a book, or nil (plus the ABS item id when it came from
--- the ABS cache).  Order:
---   1. `Book.readalong.json`, then `Book.epub.readalong.json`, next to the book;
---   2. a sidecar fetched with an Audiobookshelf download
---      (`<abs_cache>/<item>/readalong.json`), matched by the EPUB's file
---      name, then by title.  opts = { abs_cache_dir =, title = }.
function Sidecar.findSidecar(doc_path, opts)
    if type(doc_path) ~= "string" or doc_path == "" then return nil end
    local stem = doc_path:gsub("%.[Ee][Pp][Uu][Bb]$", "")
    for _, cand in ipairs({ stem .. ".readalong.json", doc_path .. ".readalong.json" }) do
        if file_exists(cand) then return cand end
    end
    local root = opts and opts.abs_cache_dir
    if not root then return nil end
    local base = norm_key(doc_path:match("([^/]+)$"))
    local title = opts.title and norm_key(opts.title)
    local by_title
    for _, item_id in ipairs(list_dirs(root)) do
        local dir = root .. "/" .. item_id
        local meta = read_meta(dir .. "/readalong.meta")
        if meta and file_exists(dir .. "/readalong.json") then
            if meta.file and norm_key(meta.file) == base then
                return dir .. "/readalong.json", meta.item_id or item_id
            end
            if title and title ~= "" and meta.title and norm_key(meta.title) == title and not by_title then
                by_title = { dir .. "/readalong.json", meta.item_id or item_id }
            end
        end
    end
    if by_title then return by_title[1], by_title[2] end
    return nil
end

--- After an Audiobookshelf download: fetch the item's `*.readalong.json`
--- (any library file ending in `.readalong.json`, e.g. dropped into the
--- audiobook's folder on the server) into `<item_dir>/readalong.json` and
--- write `readalong.meta` so findSidecar can match the EPUB later.
--- Returns the local path, or nil + reason ("none" when the item has none).
function Sidecar.fetchFromAbs(client, item, item_dir)
    if not (client and item and item.id and item_dir) then return nil, "bad arguments" end
    local files = item.libraryFiles
    if type(files) ~= "table" or #files == 0 then
        local ok, details = pcall(client.getItemDetails, client, item.id)
        files = ok and type(details) == "table" and details.libraryFiles or {}
    end
    local cands = {}
    for _, lf in ipairs(files) do
        local name = lf.metadata and lf.metadata.filename or ""
        if name:lower():match("%.readalong%.json$") and lf.ino then cands[#cands + 1] = lf end
    end
    if #cands == 0 then return nil, "none" end
    table.sort(cands, function(a, b) return a.metadata.filename < b.metadata.filename end)
    local lf = cands[1]
    local dest = item_dir .. "/readalong.json"
    local url = client.server_url .. "/api/items/" .. item.id .. "/file/" .. lf.ino .. "/download"
    local ok, err = client:downloadFile(url, dest .. ".part")
    if not ok then
        os.remove(dest .. ".part")
        return nil, "download failed: " .. tostring(err)
    end
    local sc, rerr = Sidecar.read(dest .. ".part")
    if not sc then
        os.remove(dest .. ".part")
        return nil, rerr
    end
    os.remove(dest)
    os.rename(dest .. ".part", dest)
    local f = io.open(item_dir .. "/readalong.meta", "wb")
    if f then
        local book = sc.book or {}
        f:write("item_id=", tostring(item.id), "\n")
        f:write("file=", (tostring(book.file or ""):gsub("\n", " ")), "\n")
        f:write("title=", (tostring(book.title or ""):gsub("\n", " ")), "\n")
        f:write("sha1=", tostring(book.sha1 or ""), "\n")
        f:write("source=", (lf.metadata.filename:gsub("\n", " ")), "\n")
        f:close()
    end
    logger.warn("ReadalongSidecar: fetched", lf.metadata.filename, "for", item.id)
    return dest
end

--- Minimal JSON decoder (fallback when KOReader's `json` module is absent,
--- e.g. in tests).  Handles the subset the generator writes.
function Sidecar.decodeJson(s)
    local pos = 1
    local function ws() pos = s:find("[^ \t\r\n]", pos) or #s + 1 end
    local val
    local function str()
        local out, i = {}, pos + 1
        while true do
            local c = s:sub(i, i)
            if c == "" then error("unterminated string") end
            if c == '"' then pos = i + 1 return table.concat(out) end
            if c == "\\" then
                local e = s:sub(i + 1, i + 1)
                local map = { b = "\b", f = "\f", n = "\n", r = "\r", t = "\t", ['"'] = '"', ["\\"] = "\\", ["/"] = "/" }
                if e == "u" then
                    local cp = tonumber(s:sub(i + 2, i + 5), 16)
                    i = i + 6
                    if cp >= 0xD800 and cp <= 0xDBFF and s:sub(i, i + 1) == "\\u" then
                        local lo = tonumber(s:sub(i + 2, i + 5), 16)
                        cp = 0x10000 + (cp - 0xD800) * 0x400 + (lo - 0xDC00)
                        i = i + 6
                    end
                    if cp < 0x80 then out[#out + 1] = string.char(cp)
                    elseif cp < 0x800 then out[#out + 1] = string.char(0xC0 + math.floor(cp / 64), 0x80 + cp % 64)
                    elseif cp < 0x10000 then out[#out + 1] = string.char(0xE0 + math.floor(cp / 4096), 0x80 + math.floor(cp / 64) % 64, 0x80 + cp % 64)
                    else out[#out + 1] = string.char(0xF0 + math.floor(cp / 262144), 0x80 + math.floor(cp / 4096) % 64, 0x80 + math.floor(cp / 64) % 64, 0x80 + cp % 64) end
                else
                    out[#out + 1] = map[e] or e
                    i = i + 2
                end
            else
                local j = s:find('["\\]', i) or (#s + 1)
                out[#out + 1] = s:sub(i, j - 1)
                i = j
            end
        end
    end
    function val()
        ws()
        local c = s:sub(pos, pos)
        if c == "{" then
            local t = {}
            pos = pos + 1; ws()
            if s:sub(pos, pos) == "}" then pos = pos + 1 return t end
            while true do
                ws(); local k = str(); ws()
                pos = pos + 1 -- ':'
                t[k] = val(); ws()
                local d = s:sub(pos, pos); pos = pos + 1
                if d == "}" then return t end
            end
        elseif c == "[" then
            local t = {}
            pos = pos + 1; ws()
            if s:sub(pos, pos) == "]" then pos = pos + 1 return t end
            while true do
                t[#t + 1] = val(); ws()
                local d = s:sub(pos, pos); pos = pos + 1
                if d == "]" then return t end
            end
        elseif c == '"' then return str()
        elseif s:sub(pos, pos + 3) == "true" then pos = pos + 4 return true
        elseif s:sub(pos, pos + 4) == "false" then pos = pos + 5 return false
        elseif s:sub(pos, pos + 3) == "null" then pos = pos + 4 return nil
        else
            local num = s:match("^-?%d+%.?%d*[eE]?[-+]?%d*", pos)
            if not num or num == "" then error("bad json at " .. pos) end
            pos = pos + #num
            return tonumber(num)
        end
    end
    return val()
end

local function decode(text)
    local ok_json, JSON = pcall(require, "json")
    if ok_json and JSON and JSON.decode then
        local ok, res = pcall(JSON.decode, text)
        if ok and type(res) == "table" then return res end
    end
    local ok, res = pcall(Sidecar.decodeJson, text)
    if ok and type(res) == "table" then return res end
    return nil, tostring(res)
end

function Sidecar.read(path)
    local f = io.open(path, "rb")
    if not f then return nil, "cannot open sidecar" end
    local text = f:read("*a")
    f:close()
    local sc, err = decode(text)
    if not sc then return nil, "sidecar is not valid JSON: " .. tostring(err) end
    if sc.format ~= Sidecar.FORMAT then return nil, "not a readalong sidecar" end
    if tonumber(sc.version) ~= Sidecar.VERSION then
        return nil, "unsupported sidecar version " .. tostring(sc.version)
    end
    if type(sc.cues) ~= "table" or type(sc.audio) ~= "table" or type(sc.audio.tracks) ~= "table"
        or #sc.audio.tracks == 0 then
        return nil, "sidecar has no cues or tracks"
    end
    return sc
end

-- ---------------------------------------------------------------------------
-- Audio resolution
-- ---------------------------------------------------------------------------

--- Returns paths[] (one per sidecar track) or nil, reason.
-- 1. Track files next to the sidecar (or in its `audio_dir`).
-- 2. The ABS item downloaded into the plugin's ABS cache.
function Sidecar:_resolveAudio(sc, sidecar_path, plugin_dir)
    local tracks = sc.audio.tracks
    local dir = sidecar_path:match("^(.*)/[^/]*$") or "."
    local local_dir = dir
    if type(sc.audio.audio_dir) == "string" and sc.audio.audio_dir ~= "" then
        local_dir = sc.audio.audio_dir:sub(1, 1) == "/" and sc.audio.audio_dir
            or (dir .. "/" .. sc.audio.audio_dir)
    end
    local paths, all = {}, true
    for i, tr in ipairs(tracks) do
        local p = local_dir .. "/" .. tostring(tr.file or "")
        if tr.file and file_exists(p) then paths[i] = p else all = false break end
    end
    if all then return paths, "local" end

    local item_id = sc.audio.abs_item_id
    if type(item_id) ~= "string" or item_id == "" then
        return nil, "audio files not found next to the book and no Audiobookshelf item id in the sidecar"
    end
    local cache = self.abs_cache
    if not cache and plugin_dir then
        local ok, ABSCache = pcall(dofile, plugin_dir .. "/abscache.lua")
        if ok and ABSCache then
            local ok2, c = pcall(function() return ABSCache:new{ plugin_dir = plugin_dir .. "/" } end)
            if ok2 then cache = c end
        end
    end
    local cached = cache and cache:getAudioPaths(item_id)
    if not cached or #cached == 0 then
        return nil, "download this audiobook from Audiobookshelf first (item " .. item_id .. ")"
    end
    if #cached ~= #tracks then
        return nil, string.format("Audiobookshelf item has %d audio files, sidecar expects %d",
            #cached, #tracks)
    end
    for i, p in ipairs(cached) do
        if not file_exists(p) then return nil, "downloaded audio file missing: " .. tostring(p) end
        paths[i] = p
    end
    return paths, "abs"
end

-- ---------------------------------------------------------------------------
-- Timing data
-- ---------------------------------------------------------------------------

local function field_index(sc)
    local F = {}
    for i, k in ipairs(sc.cue_fields or {}) do F[k] = i end
    -- v1 default order
    if not F.t0 then
        F = { t0 = 1, t1 = 2, spine = 3, xp = 4, bxp = 5, bw = 6, text = 7, words = 8 }
    end
    return F
end

--- Pure transform: sidecar + resolved track paths -> plugin timing entries.
function Sidecar:buildTimingData(sc, paths)
    local F = field_index(sc)
    local tracks = sc.audio.tracks
    local spine = {}
    for i, h in ipairs((sc.book and sc.book.spine) or {}) do
        spine[i] = tostring(h):match("([^/]+)$") or tostring(h)
    end
    self._spine_hrefs = spine
    self.track_offsets, self.track_paths, self.track_durations = {}, {}, {}
    for i, tr in ipairs(tracks) do
        self.track_offsets[paths[i]] = tonumber(tr.offset) or 0
        self.track_paths[i] = paths[i]
        self.track_durations[i] = tonumber(tr.duration)
    end
    self.total_duration = tonumber(sc.audio.duration)
    self.abs_item_id = (type(sc.audio.abs_item_id) == "string" and sc.audio.abs_item_id ~= "")
        and sc.audio.abs_item_id or nil

    local td, k = {}, 1
    for ci, c in ipairs(sc.cues) do
        local g0, g1 = tonumber(c[F.t0]), tonumber(c[F.t1])
        if g0 and g1 then
            while k < #tracks and g0 >= (tonumber(tracks[k + 1].offset) or math.huge) do k = k + 1 end
            while k > 1 and g0 < (tonumber(tracks[k].offset) or 0) do k = k - 1 end
            local off = tonumber(tracks[k].offset) or 0
            local dur = tonumber(tracks[k].duration)
            local s0 = g0 - off
            local s1 = g1 - off
            if dur and s1 > dur then s1 = dur end
            if s1 < s0 then s1 = s0 end
            local fid = Sidecar.ID_PREFIX .. ci
            local bxp = c[F.bxp]
            local elem_path = type(bxp) == "string"
                and bxp:match("DocFragment%[%d+%]/(.*)$") or nil
            if elem_path then elem_path = elem_path:gsub("%[1%]", "") end
            local words
            if type(c[F.words]) == "table" and #c[F.words] > 0 then
                words = {}
                for _, w in ipairs(c[F.words]) do
                    local ws = s0 + (tonumber(w[1]) or 0) / 1000
                    words[#words + 1] = {
                        start_time = ws,
                        end_time = ws + (tonumber(w[2]) or 0) / 1000,
                        text = w[3],
                    }
                end
            end
            local e = {
                start_time = s0,
                end_time = s1,
                global_start = g0,
                global_end = g1,
                text = c[F.text] or "",
                text_ref = nil,
                audio_src = tracks[k].file,
                audio_path = paths[k],
                fragment_id = fid,
                text_doc = spine[tonumber(c[F.spine]) or 0],
                spine_index = tonumber(c[F.spine]),
                xpointer = c[F.xp],
                block_xpointer = bxp,
                block_word = tonumber(c[F.bw]) or 0,
                elem_path = elem_path,
                words = words,
                sidecar = true,
            }
            td[#td + 1] = e
            self._by_frag[fid] = e
        end
    end
    self._timing = td
    return td
end

function Sidecar:loadFromEpub(epub_path, plugin_dir, progress_callback)
    local function report(phase, cur, total, detail)
        if progress_callback then
            pcall(progress_callback, { phase = phase, current = cur, total = total, detail = detail })
        end
    end
    local path = self.sidecar_path
    if not path then
        path, self.abs_item_hint = Sidecar.findSidecar(epub_path, {
            abs_cache_dir = plugin_dir and (plugin_dir .. "/abs_cache") or nil,
        })
    end
    if not path then return nil, "no readalong sidecar" end
    self.sidecar_path = path
    report("smil", 1, 2, path:match("([^/]+)$"))
    local sc, err = Sidecar.read(path)
    if not sc then return nil, err end
    -- A sidecar fetched from an ABS item belongs to that item even when the
    -- generator was run without --abs-item-id.
    if self.abs_item_hint and (type(sc.audio.abs_item_id) ~= "string" or sc.audio.abs_item_id == "") then
        sc.audio.abs_item_id = self.abs_item_hint
    end
    local paths, how = self:_resolveAudio(sc, path, plugin_dir)
    if not paths then return nil, how end
    self.audio_source = how
    local td = self:buildTimingData(sc, paths)
    if #td == 0 then return nil, "sidecar has no usable cues" end
    -- Chapter titles from the book's NCX (reuses the overlay parser helpers).
    pcall(function()
        local EMO = dofile((plugin_dir or ".") .. "/epubmediaoverlay.lua")
        local p = EMO:new()
        local opf_path = p:_findOpfPath(epub_path)
        local opf_xml = opf_path and p:_extractFromZip(epub_path, opf_path)
        if opf_xml then
            self._chapter_titles = p:_loadChapterTitles(epub_path, opf_xml, opf_path:match("^(.*)/") or "")
        end
    end)
    self.sidecar = { title = sc.book and sc.book.title, stats = sc.stats, created = sc.created }
    report("done", #td, 2, string.format("%d cues, %d audio (%s)", #td, #paths, how))
    logger.warn("ReadalongSidecar: loaded", #td, "cues,", #paths, "tracks from", how)
    return td
end

function Sidecar:getCacheDir() return nil end
function Sidecar:cleanupOldCaches() end

--- Entry at a global (ABS) book time; for "resume at ABS position".
function Sidecar:entryAtGlobalTime(t)
    local td = self._timing
    if not td or #td == 0 or not t then return nil end
    local lo, hi, best = 1, #td, 1
    while lo <= hi do
        local mid = math.floor((lo + hi) / 2)
        if td[mid].global_start <= t then best = mid lo = mid + 1 else hi = mid - 1 end
    end
    return td[best]
end

function Sidecar:globalPosition(path, local_pos)
    local off = self.track_offsets and self.track_offsets[path]
    if not off then return nil end
    return off + (local_pos or 0)
end

function Sidecar.isSidecarFragment(fid)
    return type(fid) == "string" and fid:sub(1, #Sidecar.ID_PREFIX) == Sidecar.ID_PREFIX
end

-- ---------------------------------------------------------------------------
-- XPointer resolution against crengine
-- ---------------------------------------------------------------------------

local function fold(s)
    s = tostring(s or ""):lower()
    -- keep ASCII alnum and any multibyte (accented) characters
    return (s:gsub("[%z\1-\47\58-\64\91-\96\123-\127]", ""))
end

-- Does crengine text starting at xp begin with the cue's first words?
local function starts_cue(doc, xp, want)
    if not xp or want == "" then return false end
    local e1, e2
    pcall(function() e1 = doc:getNextVisibleWordEnd(xp) end)
    if not e1 then return false end
    pcall(function() e2 = doc:getNextVisibleWordEnd(e1) end)
    local drawn = ""
    pcall(function() drawn = doc:getTextFromXPointers(xp, e2 or e1) or "" end)
    local d = fold(drawn)
    if d == "" then return false end
    if #d > #want then d = d:sub(1, #want) end
    return want:sub(1, #d) == d and #d >= math.min(3, #want)
end

--- Resolve a sidecar entry to the crengine xpointer of its first word.
-- 1. The generator's text-node xpointer, nudged +-3 chars for whitespace
--    differences.  2. Walk the words of the enclosing block and take the
--    occurrence of the cue's opening words closest to the recorded word
--    index.  Returns nil when neither lands (callers then fall back to the
--    plugin's on-page text matching).
function Sidecar.resolveStartXPointer(doc, e)
    if not doc or not e then return nil end
    local want = fold(e.text)
    if #want > 24 then want = want:sub(1, 24) end
    local function norm(xp)
        local ok, nx = pcall(function() return doc:getNormalizedXPointer(xp) end)
        if ok and nx and nx ~= false then return nx end
        return nil
    end
    if type(e.xpointer) == "string" then
        local base, off = e.xpointer:match("^(.*)%.(%d+)$")
        off = tonumber(off)
        if base and off then
            for _, d in ipairs({ 0, -1, 1, -2, 2, -3, 3 }) do
                if off + d >= 0 then
                    local nx = norm(base .. "." .. (off + d))
                    if nx and starts_cue(doc, nx, want) then return nx end
                end
            end
        end
    end
    local bxp = type(e.block_xpointer) == "string" and norm(e.block_xpointer)
    if not bxp then return nil end
    local prefix = bxp:gsub("/text%(%).*$", "")
    local best, best_d
    local cur = bxp
    for i = 0, 1500 do
        if starts_cue(doc, cur, want) then
            local d = math.abs(i - (e.block_word or 0))
            if not best or d < best_d then best, best_d = cur, d end
            if i > (e.block_word or 0) then break end
        end
        local nxt
        pcall(function() nxt = doc:getNextVisibleWordStart(cur) end)
        if not nxt or nxt == cur then break end
        -- stay inside the block ("p[1]" must not match "p[10]")
        if nxt ~= prefix and nxt:sub(1, #prefix + 1) ~= prefix .. "/"
            and nxt:sub(1, #prefix + 1) ~= prefix .. "." then break end
        cur = nxt
    end
    return best
end

--- HighlightManager / MediaSync hook: fragment id -> crengine xpointer.
function Sidecar:resolveFragment(doc, fragment_id)
    local cached = self._xp_cache[fragment_id]
    if cached ~= nil then return cached or nil end
    local e = self._by_frag[fragment_id]
    local xp = e and Sidecar.resolveStartXPointer(doc, e)
    self._xp_cache[fragment_id] = xp or false
    return xp
end

return Sidecar
