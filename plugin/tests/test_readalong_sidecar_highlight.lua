#!/usr/bin/env luajit
-- Integration: HighlightManager + read-along sidecar entries on a fake
-- crengine document.  Checks that a sidecar cue ("ra:<n>", no ids in the
-- EPUB) resolves to the right start xpointer and that the existing word-walk
-- grows the range to exactly the cue's sentence, clamped before the next cue.
-- Run from the repo root: luajit dev/test_readalong_sidecar_highlight.lua
local here = (arg and arg[0]):gsub("[^/]+$", "")
package.preload["logger"] = function()
    return setmetatable({}, { __index = function() return function() end end })
end
package.preload["audiobook_gettext"] = function() return function(s) return s end end
package.preload["ffi/blitbuffer"] = function() return {} end
package.preload["device"] = function()
    return { screen = { getWidth = function() return 1000 end, getHeight = function() return 1000 end },
             isKindle = function() return false end }
end
package.preload["ui/geometry"] = function() return { new = function(_, o) return o end } end
package.preload["ui/uimanager"] = function() return setmetatable({}, { __index = function() return function() end end }) end

local Sidecar = dofile(here .. "../readalongsidecar.lua")
local HM = dofile(here .. "../highlightmanager.lua")
local FakeDoc = dofile(here .. "fake_crengine_doc.lua")

local failures, checks = {}, 0
local function eq(a, b, label)
    checks = checks + 1
    if a ~= b then failures[#failures + 1] = string.format("%s: expected %s, got %s", label, tostring(b), tostring(a)) end
end

local base = "/body/DocFragment[2]/body/div[1]"
local paras = {
    "One dollar and eighty-seven cents. That was all. And sixty cents of it was in pennies.",
    "The cat sat. The cat sat again.",
}
-- crengine-side text has the same words; the generator's offsets are given
-- one too high for paragraph 1 (as if crengine trimmed a leading space).
local doc = FakeDoc(paras, "/body/DocFragment[2]/body/div")
local sc = {
    format = "koreader-readalong", version = 1,
    book = { spine = { "cover.xhtml", "ch1.xhtml" } },
    audio = { abs_item_id = "li_1", duration = 60, tracks = { { index = 1, file = "a.mp3", duration = 60, offset = 0 } } },
    cues = {
        { 1, 3, 2, base .. "/p[1]/text().1", base .. "/p[1]", 0, "One dollar and eighty-seven cents.", {} },
        { 3, 4, 2, base .. "/p[1]/text().36", base .. "/p[1]", 5, "That was all.", {} },
        { 4, 7, 2, base .. "/p[1]/text().50", base .. "/p[1]", 8, "And sixty cents of it was in pennies.", {} },
        { 8, 9, 2, "/broken", base .. "/p[2]", 0, "The cat sat.", {} },
        { 9, 11, 2, "/broken", base .. "/p[2]", 3, "The cat sat again.", {} },
    },
}
-- generator writes explicit [1] indices; the fake doc addresses without them
for _, c in ipairs(sc.cues) do
    c[4] = c[4]:gsub("div%[1%]", "div"); c[5] = c[5]:gsub("div%[1%]", "div")
end
local parser = Sidecar:new()
local td = parser:buildTimingData(sc, { "/x/a.mp3" })
local plugin = { _smil_parser = parser, getSetting = function(_, _, d) return d end }
local hm = HM:new{ plugin = plugin, ui = { document = doc, rolling = true } }
plugin.media_sync = { timing_data = td, _current_sentence_idx = 1,
    _getExpectedDocFragmentIndex = function() return 2 end }

local function range_text(i)
    local e = td[i]
    local xp0, xp1 = hm:_fragmentTextRange(doc, e.fragment_id, e)
    if not xp0 then return nil end
    return doc:getTextFromXPointers(xp0, xp1)
end
for i, want in ipairs({ "One dollar and eighty-seven cents.", "That was all.",
        "And sixty cents of it was in pennies.", "The cat sat.", "The cat sat again." }) do
    plugin.media_sync._current_sentence_idx = i
    eq(range_text(i), want, "cue " .. i .. " highlight range")
end

-- word highlight inside a sidecar cue: sidecar tokens "eighty" / "seven"
-- both land on crengine's single word "eighty-seven"
function doc:getScreenBoxesFromPositions(a, b) return { { x = 0, y = 0, w = 1, h = 1, text = self:getTextFromXPointers(a, b) } } end
function doc:getCurrentPage() return 1 end
local painted
hm._ensureViewModule = function() end
hm._refreshWordHighlight = function(_, boxes) painted = boxes[1].text end
local e1 = td[1]
e1.words = {
    { start_time = 1.0, end_time = 1.2, text = "One" },
    { start_time = 1.2, end_time = 1.5, text = "dollar" },
    { start_time = 1.5, end_time = 1.6, text = "and" },
    { start_time = 1.6, end_time = 1.9, text = "eighty" },
    { start_time = 1.9, end_time = 2.2, text = "seven" },
    { start_time = 2.2, end_time = 2.6, text = "cents." },
}
plugin.getSetting = function(_, k, d) if k == "highlight_words" then return true end return d end
for i, want in ipairs({ "One", "dollar", "and", "eighty-seven", "eighty-seven", "cents." }) do
    painted = nil
    hm:highlightWord(e1.words[i], { sentences = { e1 } })
    eq(painted, want, "word " .. i .. " box")
end

if #failures > 0 then
    print(string.format("FAIL %d/%d", #failures, checks))
    for _, f in ipairs(failures) do print("  " .. f) end
    os.exit(1)
end
print(string.format("ok %d checks", checks))
