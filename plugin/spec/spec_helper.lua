-- Shared setup for the plugin specs (busted, or plugin/spec/run.lua).
-- Stubs the KOReader modules the overlay files require at load time, so the
-- specs run on plain Lua 5.1 / LuaJIT without KOReader or the upstream plugin.
if rawget(_G, "__READALONG_SPEC_HELPER") then return _G.__READALONG_SPEC_HELPER end

local H = {}
H.spec_dir = (debug.getinfo(1, "S").source:match("^@(.*[/\\])") or "./")
H.plugin_dir = H.spec_dir .. "../"
H.overlay_dir = H.plugin_dir .. "overlay/"
H.tests_dir = H.plugin_dir .. "tests/"

-- A table whose every field is a no-op function (and callable itself).
local function permissive(name)
    local t
    t = setmetatable({ _stub = name }, {
        __index = function(_, k)
            if k == "screen" then return t end
            return function() return nil end
        end,
        __call = function() return t end,
    })
    return t
end
H.permissive = permissive

local logs = {}
H.logs = logs
local logger = setmetatable({}, {
    __index = function(_, level)
        return function(...)
            local parts = {}
            for i = 1, select("#", ...) do parts[#parts + 1] = tostring((select(i, ...))) end
            logs[#logs + 1] = level .. ": " .. table.concat(parts, " ")
        end
    end,
})

local stubs = {
    logger = function() return logger end,
    audiobook_gettext = function() return function(s) return s end end,
    device = function()
        local d = permissive("device")
        rawset(d, "screen", { getWidth = function() return 1000 end, getHeight = function() return 1000 end,
                              scaleBySize = function(_, v) return v end })
        rawset(d, "isKindle", function() return false end)
        return d
    end,
    ["ui/geometry"] = function() return { new = function(_, o) return o or {} end } end,
    ["ui/time"] = function()
        return { now = function() return os.time() * 1e6 end, s = function(v) return v * 1e6 end,
                 to_s = function(v) return v / 1e6 end }
    end,
}
for name, fn in pairs(stubs) do package.preload[name] = fn end

-- Anything else KOReader-ish (ui/*, ffi/*, libs/*, apps/*, frontend modules)
-- resolves to a permissive stub instead of failing the require.
local function stub_loader(name)
    if name:match("^ui/") or name:match("^ffi/") or name:match("^apps/") or name:match("^document/")
        or name == "datastorage" or name == "luasettings" or name == "gettext" then
        return function() return permissive(name) end
    end
    return "\n\tno KOReader stub for '" .. name .. "'"
end
local loaders = package.searchers or package.loaders
table.insert(loaders, stub_loader)

function H.load_overlay(file)
    return dofile(H.overlay_dir .. file)
end

-- mediasync.lua does dofile(<its dir> .. "utils.lua"); utils.lua is upstream
-- (not in overlay/), so load a copy next to a stub utils.lua.
local _ms
function H.load_mediasync()
    if _ms then return _ms end
    local dir = H.tmpdir()
    local src = assert(io.open(H.overlay_dir .. "mediasync.lua", "rb")):read("*a")
    H.write(dir .. "/mediasync.lua", src)
    H.write(dir .. "/utils.lua", "return setmetatable({}, { __index = function() return function() end end })\n")
    _ms = dofile(dir .. "/mediasync.lua")
    return _ms
end

local tmpdirs = {}
function H.tmpdir()
    local p = os.tmpname()
    os.remove(p)
    os.execute('mkdir -p "' .. p .. '"')
    tmpdirs[#tmpdirs + 1] = p
    return p
end

function H.mkdir(p) os.execute('mkdir -p "' .. p .. '"') end

function H.write(path, s)
    local f = assert(io.open(path, "wb"))
    f:write(s)
    f:close()
    return path
end

function H.exists(path)
    local f = io.open(path, "rb")
    if f then f:close() return true end
    return false
end

function H.read(path)
    local f = io.open(path, "rb")
    if not f then return nil end
    local s = f:read("*a")
    f:close()
    return s
end

function H.cleanup()
    for _, p in ipairs(tmpdirs) do os.execute('rm -rf "' .. p .. '"') end
    tmpdirs = {}
end

function H.near(a, b, tol)
    return type(a) == "number" and math.abs(a - b) <= (tol or 1e-6)
end

--- A two-track sidecar (global times; track 2 starts at 100 s).
function H.sample_sidecar()
    return {
        format = "koreader-readalong", version = 1,
        book = { title = "Sample", file = "Sample.epub", spine = { "OEBPS/cover.xhtml", "OEBPS/ch1.xhtml", "ch2.xhtml" } },
        audio = { abs_item_id = "li_123", duration = 200,
            tracks = { { index = 1, file = "01.mp3", duration = 100, offset = 0 },
                       { index = 2, file = "02.mp3", duration = 100, offset = 100 } } },
        cue_fields = { "t0", "t1", "spine", "xp", "bxp", "bw", "text", "words" },
        cues = {
            { 10.0, 12.5, 2, "/body/DocFragment[2]/body/div[1]/p[1]/text().0", "/body/DocFragment[2]/body/div[1]/p[1]", 0,
              "Hello there world.", { { 0, 400, "Hello" }, { 500, 300, "there" }, { 900, 1000, "world." } } },
            { 13.0, 15.0, 2, "/body/DocFragment[2]/body/div[1]/p[1]/text().19", "/body/DocFragment[2]/body/div[1]/p[1]", 3,
              "Second sentence.", { { 0, 700, "Second" }, { 800, 1100, "sentence." } } },
            { 98.0, 101.0, 2, "/body/DocFragment[2]/body/div[1]/p[2]/text().0", "/body/DocFragment[2]/body/div[1]/p[2]", 0,
              "Across the boundary.", {} },
            { 150.0, 152.0, 3, "/body/DocFragment[3]/body/p[3]/text().5", "/body/DocFragment[3]/body/p[3]", 1,
              "Second track.", { { 0, 500, "Second" }, { 600, 900, "track." } } },
        },
    }
end

--- Minimal JSON encoder for writing sidecar fixtures.
function H.json(v)
    local t = type(v)
    if t == "table" then
        if #v > 0 or next(v) == nil then
            local out = {}
            for i = 1, #v do out[i] = H.json(v[i]) end
            return "[" .. table.concat(out, ",") .. "]"
        end
        local keys = {}
        for k in pairs(v) do keys[#keys + 1] = k end
        table.sort(keys)
        local out = {}
        for _, k in ipairs(keys) do out[#out + 1] = string.format("%q", k) .. ":" .. H.json(v[k]) end
        return "{" .. table.concat(out, ",") .. "}"
    elseif t == "string" then
        return '"' .. v:gsub('[%c"\\]', function(c)
            return ({ ['"'] = '\\"', ["\\"] = "\\\\", ["\n"] = "\\n" })[c] or string.format("\\u%04x", c:byte())
        end) .. '"'
    elseif t == "number" then
        return (v == math.floor(v) and v < 1e15) and string.format("%d", v) or string.format("%.17g", v)
    elseif v == nil then
        return "null"
    end
    return tostring(v)
end

_G.__READALONG_SPEC_HELPER = H
return H
