-- Minimal stand-in for a crengine document (rolling mode) used by the
-- read-along sidecar tests: paragraphs of text addressed by
-- "<base>/p[N]/text().OFFSET" xpointers.
-- Paragraph texts as crengine would hold them; offsets are 0-based.
local function FakeDoc(paras, base)
    local words = {}  -- ordered { p=, s=, e= }
    for pi, txt in ipairs(paras) do
        for s, w in txt:gmatch("()(%S+)") do words[#words + 1] = { p = pi, s = s - 1, e = s - 1 + #w } end
    end
    local function px(pi) return base .. "/p[" .. pi .. "]" end
    local function xp(pi, off) return px(pi) .. "/text()." .. off end
    local function parse(x)
        local pi, off = x:match("/p%[(%d+)%]/text%(%)%.(%d+)$")
        if pi then return tonumber(pi), tonumber(off) end
        pi = x:match("/p%[(%d+)%]$")
        if pi then return tonumber(pi), nil end
    end
    local d = {}
    function d:getNormalizedXPointer(x)
        local pi, off = parse(x)
        if not pi or not paras[pi] then return false end
        if off and off > #paras[pi] then return false end
        return x
    end
    local function word_idx_at_or_after(pi, off)
        for i, w in ipairs(words) do
            if w.p > pi or (w.p == pi and w.e > (off or -1)) then return i end
        end
    end
    function d:getNextVisibleWordStart(x)
        local pi, off = parse(x)
        if not off then local i = word_idx_at_or_after(pi, -1) return i and xp(words[i].p, words[i].s) end
        for i, w in ipairs(words) do
            if w.p > pi or (w.p == pi and w.s > off) then return xp(w.p, w.s) end
        end
    end
    function d:getNextVisibleWordEnd(x)
        local pi, off = parse(x)
        for i, w in ipairs(words) do
            if w.p > pi or (w.p == pi and w.e > (off or -1)) then return xp(w.p, w.e) end
        end
    end
    function d:getTextFromXPointers(a, b)
        local p0, o0 = parse(a); local p1, o1 = parse(b)
        o0 = o0 or 0
        if p0 == p1 then return paras[p0]:sub(o0 + 1, o1) end
        local out = { paras[p0]:sub(o0 + 1) }
        for k = p0 + 1, p1 - 1 do out[#out + 1] = paras[k] end
        out[#out + 1] = paras[p1]:sub(1, o1)
        return table.concat(out, " ")
    end
    local function key(x)
        local pi, off = parse(x)
        return pi * 100000 + (off or -1)
    end
    function d:compareXPointers(a, b)
        local ka, kb = key(a), key(b)
        if ka == kb then return 0 end
        return ka < kb and 1 or -1
    end
    function d:getPrevVisibleWordEnd(x)
        local k, best = key(x), nil
        for _, w in ipairs(words) do
            if w.p * 100000 + w.e < k then best = xp(w.p, w.e) end
        end
        return best
    end
    return d
end
return FakeDoc
