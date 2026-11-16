#!/usr/bin/env luajit
-- Minimal busted-compatible runner for the plugin specs, for machines without
-- busted (e.g. a bare LuaJIT, or lupa via tools/luajit.py):
--   luajit plugin/spec/run.lua [spec files...]
--   python tools/luajit.py plugin/spec/run.lua
-- Supports describe/it/pending/before_each/after_each and the luassert subset
-- the specs use.  With busted installed, `busted` (see .busted) runs the same files.
local dir = ((arg and arg[0]) or ""):match("^(.*[/\\])") or "./"

local function fmt(v)
    if type(v) == "string" then return string.format("%q", v) end
    return tostring(v)
end

local function deep_eq(a, b)
    if a == b then return true end
    if type(a) ~= "table" or type(b) ~= "table" then return false end
    for k, v in pairs(a) do if not deep_eq(v, b[k]) then return false end end
    for k in pairs(b) do if a[k] == nil then return false end end
    return true
end

local function fail(msg) error({ assertion = msg }, 3) end
local A = {}
function A.equal(e, a, m) if e ~= a then fail((m or "") .. " expected " .. fmt(e) .. ", got " .. fmt(a)) end end
function A.same(e, a, m) if not deep_eq(e, a) then fail((m or "") .. " tables differ") end end
function A.is_nil(a, m) if a ~= nil then fail((m or "") .. " expected nil, got " .. fmt(a)) end end
function A.is_not_nil(a, m) if a == nil then fail((m or "") .. " expected a value, got nil") end end
function A.is_true(a, m) if a ~= true then fail((m or "") .. " expected true, got " .. fmt(a)) end end
function A.is_false(a, m) if a ~= false then fail((m or "") .. " expected false, got " .. fmt(a)) end end
function A.truthy(a, m) if not a then fail((m or "") .. " expected truthy, got " .. fmt(a)) end end
function A.falsy(a, m) if a then fail((m or "") .. " expected falsy, got " .. fmt(a)) end end
function A.matches(p, s, m)
    if type(s) ~= "string" or not s:find(p) then fail((m or "") .. " " .. fmt(s) .. " does not match " .. fmt(p)) end
end
function A.near(e, a, tol, m)
    if type(a) ~= "number" or math.abs(e - a) > tol then fail((m or "") .. " expected " .. e .. " +- " .. tol .. ", got " .. fmt(a)) end
end
function A.has_error(fn, m) if pcall(fn) then fail((m or "") .. " expected an error") end end
A.is_truthy, A.is_falsy, A.has_no_error = A.truthy, A.falsy, function(fn) local ok, e = pcall(fn) if not ok then fail(tostring(e)) end end
local base_assert = assert
local assert_t = setmetatable({}, {
    __call = function(_, c, m, ...) if not c then fail(m or "assertion failed!") end return c, m, ... end,
    __index = function(t, k)
        if k == "are" or k == "is" or k == "has" then return t end
        local f = A[k] or A["is_" .. k]
        if not f then base_assert(false, "runner: unsupported assertion " .. tostring(k)) end
        return f
    end,
})

-- ── suite structure ─────────────────────────────────────────────────────
local root = { name = "", children = {}, before = {}, after = {} }
local cur = root
function describe(name, fn)
    local node = { name = name, children = {}, before = {}, after = {}, parent = cur }
    cur.children[#cur.children + 1] = node
    local prev = cur; cur = node; fn(); cur = prev
end
function it(name, fn) cur.children[#cur.children + 1] = { name = name, test = fn, parent = cur } end
function pending(name) cur.children[#cur.children + 1] = { name = name, pending = true, parent = cur } end
function before_each(fn) cur.before[#cur.before + 1] = fn end
function after_each(fn) cur.after[#cur.after + 1] = fn end
_G.assert = assert_t

local files = {}
for i = 1, #(arg or {}) do files[#files + 1] = arg[i] end
if #files == 0 then
    local h = io.popen('ls -1 "' .. dir .. '" 2>/dev/null')
    if h then
        for name in h:lines() do if name:match("_spec%.lua$") then files[#files + 1] = dir .. name end end
        h:close()
    end
    table.sort(files)
end

local passed, failed, skipped, errors = 0, 0, 0, {}
for _, f in ipairs(files) do
    local chunk, err = loadfile(f)
    if not chunk then
        errors[#errors + 1] = f .. ": " .. tostring(err)
    else
        local ok, e = pcall(chunk)
        if not ok then errors[#errors + 1] = f .. ": " .. tostring(type(e) == "table" and e.assertion or e) end
    end
end

local function chain(node, key)
    local list, n = {}, node
    while n do table.insert(list, 1, n) n = n.parent end
    local out = {}
    for _, x in ipairs(list) do for _, fn in ipairs(x[key] or {}) do out[#out + 1] = fn end end
    return out
end

local function run(node, prefix)
    for _, c in ipairs(node.children) do
        local name = (prefix ~= "" and (prefix .. " ") or "") .. c.name
        if c.children then
            run(c, name)
        elseif c.pending then
            skipped = skipped + 1
        else
            local ok, e = true, nil
            for _, b in ipairs(chain(node, "before")) do
                ok, e = pcall(b); if not ok then break end
            end
            if ok then ok, e = pcall(c.test) end
            local afters = chain(node, "after")
            for i = #afters, 1, -1 do pcall(afters[i]) end
            if ok then
                passed = passed + 1
            else
                failed = failed + 1
                local msg = type(e) == "table" and e.assertion or tostring(e)
                errors[#errors + 1] = name .. "\n      " .. msg
            end
        end
    end
end
run(root, "")

for _, e in ipairs(errors) do print("FAIL " .. e) end
print(string.format("%d passed, %d failed, %d pending, %d errors (%s)", passed, failed, skipped,
    #errors - failed, _VERSION .. (rawget(_G, "jit") and (" / " .. jit.version) or "")))
if #errors > 0 then os.exit(1) end
