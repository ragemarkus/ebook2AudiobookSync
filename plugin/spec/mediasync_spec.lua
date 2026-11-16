-- mediasync.lua: which sentence / word is being read at a playback position,
-- and global book time -> playlist part, fed with sidecar timing data.
local H = dofile((debug.getinfo(1, "S").source:match("^@(.*[/\\])") or "./") .. "spec_helper.lua")
local Sidecar = H.load_overlay("readalongsidecar.lua")
local MediaSync = H.load_mediasync()

local function track_entries(td, path)
    local out = {}
    for _, e in ipairs(td) do if e.audio_path == path then out[#out + 1] = e end end
    return out
end

describe("MediaSync with sidecar timing data", function()
    local ms, td
    before_each(function()
        td = Sidecar:new():buildTimingData(H.sample_sidecar(), { "/a/01.mp3", "/a/02.mp3" })
        ms = MediaSync:new()
        ms.timing_data = track_entries(td, "/a/01.mp3")   -- the playing track's cues
    end)

    it("finds the sentence at a track-local position", function()
        assert.are.equal(1, ms:_findSentenceAtTime(10.0))
        assert.are.equal(1, ms:_findSentenceAtTime(12.4))
        assert.are.equal(2, ms:_findSentenceAtTime(14.9))
        assert.are.equal(3, ms:_findSentenceAtTime(99.0))
    end)

    it("keeps the previous sentence in gaps, and clamps before/after", function()
        assert.are.equal(1, ms:_findSentenceAtTime(12.7))   -- between cue 1 (ends 12.5) and cue 2 (13.0)
        assert.are.equal(1, ms:_findSentenceAtTime(0.5))    -- before the first cue
        assert.are.equal(3, ms:_findSentenceAtTime(150))    -- after the last cue
        ms.timing_data = {}
        assert.is_nil(ms:_findSentenceAtTime(3))
    end)

    it("finds the word being read, or the closest one between words", function()
        local words = ms.timing_data[1].words            -- Hello 10.0-10.4, there 10.5-10.8, world. 10.9-11.9
        assert.are.equal(1, ms:_findWordAtTime(words, 10.1))
        assert.are.equal(2, ms:_findWordAtTime(words, 10.6))
        assert.are.equal(3, ms:_findWordAtTime(words, 11.5))
        assert.are.equal(1, ms:_findWordAtTime(words, 10.42)) -- gap: nearer the end of "Hello"
        assert.are.equal(2, ms:_findWordAtTime(words, 10.48)) -- gap: nearer the start of "there"
        assert.are.equal(3, ms:_findWordAtTime(words, 30))    -- far past: last word
    end)

    it("uses track-local times on the second track", function()
        ms.timing_data = track_entries(td, "/a/02.mp3")
        local i = ms:_findSentenceAtTime(50.7)              -- global 150.7
        assert.are.equal("Second track.", ms.timing_data[i].text)
        assert.are.equal(2, ms:_findWordAtTime(ms.timing_data[i].words, 50.7))
    end)

    it("maps a global book time onto the playlist part and local offset", function()
        ms.playlist_files = { { path = "/a/01.mp3" }, { path = "/a/02.mp3" } }
        ms._playlist_durations = { 100, 100 }
        local idx, off = ms:_playlistResolveGlobalTime(150)
        assert.are.equal(2, idx); assert.are.equal(50, off)
        idx, off = ms:_playlistResolveGlobalTime(0)
        assert.are.equal(1, idx); assert.are.equal(0, off)
        idx = ms:_playlistResolveGlobalTime(100)
        assert.are.equal(2, idx)
        assert.is_nil(ms:_playlistResolveGlobalTime(250))    -- past the end
        ms._playlist_durations = { 100 }                      -- unknown durations
        assert.is_nil(ms:_playlistResolveGlobalTime(10))
    end)
end)
