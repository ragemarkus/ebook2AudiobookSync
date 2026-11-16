-- readalongsidecar.lua: mapping a cue to the crengine xpointer of its first
-- word (fake crengine document from plugin/tests/fake_crengine_doc.lua).
local H = dofile((debug.getinfo(1, "S").source:match("^@(.*[/\\])") or "./") .. "spec_helper.lua")
local Sidecar = H.load_overlay("readalongsidecar.lua")
local FakeDoc = dofile(H.tests_dir .. "fake_crengine_doc.lua")

local base = "/body/DocFragment[3]/body/div"
local function entry(xp, bxp, bw, text) return { xpointer = xp, block_xpointer = bxp, block_word = bw, text = text } end

describe("readalong sidecar: cue -> crengine xpointer", function()
    local doc
    before_each(function()
        doc = FakeDoc({
            "One dollar and eighty-seven cents. That was all.",
            "The cat sat. The cat sat again. The dog ran.",
        }, base)
    end)

    it("uses the generator's xpointer when it lands on the first word", function()
        assert.are.equal(base .. "/p[1]/text().35",
            Sidecar.resolveStartXPointer(doc, entry(base .. "/p[1]/text().35", base .. "/p[1]", 5, "That was all.")))
    end)

    it("nudges the offset for whitespace differences", function()
        assert.are.equal(base .. "/p[1]/text().35",
            Sidecar.resolveStartXPointer(doc, entry(base .. "/p[1]/text().36", base .. "/p[1]", 5, "That was all.")))
        assert.are.equal(base .. "/p[1]/text().35",
            Sidecar.resolveStartXPointer(doc, entry(base .. "/p[1]/text().37", base .. "/p[1]", 5, "That was all.")))
    end)

    it("walks the block and picks the occurrence nearest the recorded word index", function()
        assert.are.equal(base .. "/p[2]/text().13",
            Sidecar.resolveStartXPointer(doc, entry(base .. "/p[9]/text().0", base .. "/p[2]", 3, "The cat sat again.")))
        assert.are.equal(base .. "/p[2]/text().32",
            Sidecar.resolveStartXPointer(doc, entry(nil, base .. "/p[2]", 6, "The dog ran.")))
    end)

    it("returns nil when nothing matches (caller falls back to on-page text matching)", function()
        assert.is_nil(Sidecar.resolveStartXPointer(doc, entry(nil, base .. "/p[2]", 0, "Nothing like this")))
        assert.is_nil(Sidecar.resolveStartXPointer(doc, nil))
        assert.is_nil(Sidecar.resolveStartXPointer(nil, entry(nil, base .. "/p[2]", 0, "The")))
    end)

    it("never walks out of its block (p[1] must not match text in p[10])", function()
        local many = {}
        for i = 1, 10 do many[i] = "Para " .. i .. " text." end
        many[10] = "Target words here."
        local doc10 = FakeDoc(many, base)
        assert.is_nil(Sidecar.resolveStartXPointer(doc10, entry(nil, base .. "/p[1]", 0, "Target words here.")))
        assert.is_not_nil(Sidecar.resolveStartXPointer(doc10, entry(nil, base .. "/p[10]", 0, "Target words here.")))
    end)

    it("resolveFragment resolves ra:<n> ids from timing data and caches misses too", function()
        local p = Sidecar:new()
        p._by_frag["ra:1"] = entry(base .. "/p[1]/text().0", base .. "/p[1]", 0, "One dollar and")
        p._by_frag["ra:2"] = entry(nil, base .. "/p[2]", 0, "Not in the book")
        assert.are.equal(base .. "/p[1]/text().0", p:resolveFragment(doc, "ra:1"))
        assert.are.equal(base .. "/p[1]/text().0", p._xp_cache["ra:1"])
        assert.is_nil(p:resolveFragment(doc, "ra:2"))
        assert.is_false(p._xp_cache["ra:2"])
        assert.is_nil(p:resolveFragment(doc, "ra:99"))
    end)
end)
