--[[--
Audiobookshelf REST API Client
Wraps the ABS API using socket.http + ltn12.
Follows the HTTP patterns from downloader.lua and updater.lua.

@module absclient
--]]

local logger = require("logger")
local _ = require("audiobook_gettext")

local ABSClient = {}

function ABSClient:new(o)
    o = o or {}
    setmetatable(o, self)
    self.__index = self

    -- Normalize server URL: strip trailing slash
    if o.server_url then
        o.server_url = o.server_url:gsub("/+$", "")
    end

    o.token = o.token or ""
    o.user_id = o.user_id or ""

    -- [cf-access patch] Cloudflare Access service-token credentials.
    -- Callers may pass cf_access_client_id / cf_access_client_secret; if they
    -- don't, fall back to the plugin settings, then to abs_access.lua.
    if o.cf_access_client_id == nil or o.cf_access_client_secret == nil then
        local id, secret = ABSClient.loadAccessCredentials()
        if o.cf_access_client_id == nil then o.cf_access_client_id = id end
        if o.cf_access_client_secret == nil then o.cf_access_client_secret = secret end
    end
    o.cf_access_client_id = ABSClient._trim(o.cf_access_client_id)
    o.cf_access_client_secret = ABSClient._trim(o.cf_access_client_secret)

    return o
end

-- ---------------------------------------------------------------------------
-- [cf-access patch] Cloudflare Access service-token support
-- ---------------------------------------------------------------------------
-- When an Audiobookshelf server sits behind Cloudflare Access, every request
-- must carry CF-Access-Client-Id / CF-Access-Client-Secret. These are optional:
-- when either value is blank the client behaves exactly like upstream.

ABSClient.ACCESS_SETTINGS_KEY = "audiobook_settings"
ABSClient.ACCESS_ID_KEY = "abs_cf_access_client_id"
ABSClient.ACCESS_SECRET_KEY = "abs_cf_access_client_secret"
ABSClient.ACCESS_CONFIG_FILE = "abs_access.lua"

function ABSClient._trim(v)
    if type(v) ~= "string" then return "" end
    return (v:gsub("^%s+", ""):gsub("%s+$", ""))
end

local function plugin_dir()
    local src = debug.getinfo(1, "S").source or ""
    return src:match("^@(.*/)") or "./"
end

--[[--
Find Access credentials. Order: KOReader settings (set from the plugin menu),
then <plugin dir>/abs_access.lua returning
{ client_id = "...", client_secret = "..." }.
@return string, string  client_id, client_secret ("" when not configured)
--]]
function ABSClient.loadAccessCredentials()
    local id, secret = "", ""
    if G_reader_settings and G_reader_settings.readSetting then
        local ok, settings = pcall(function()
            return G_reader_settings:readSetting(ABSClient.ACCESS_SETTINGS_KEY)
        end)
        if ok and type(settings) == "table" then
            id = ABSClient._trim(settings[ABSClient.ACCESS_ID_KEY])
            secret = ABSClient._trim(settings[ABSClient.ACCESS_SECRET_KEY])
        end
    end
    if id == "" or secret == "" then
        local path = plugin_dir() .. ABSClient.ACCESS_CONFIG_FILE
        local f = io.open(path, "r")
        if f then
            f:close()
            local ok, cfg = pcall(dofile, path)
            if ok and type(cfg) == "table" then
                id = ABSClient._trim(cfg.client_id)
                secret = ABSClient._trim(cfg.client_secret)
            else
                logger.warn("ABSClient: could not read", path, tostring(cfg))
            end
        end
    end
    return id, secret
end

-- [cf-access patch] nil = socket.http default (auto-follow, upstream
-- behaviour); false = don't auto-follow when an Access token is set.
function ABSClient:_followRedirectFlag()
    if self:hasAccessCredentials() then return false end
    return nil
end

function ABSClient:hasAccessCredentials()
    return self.cf_access_client_id ~= "" and self.cf_access_client_secret ~= ""
end

local function url_host(url)
    if type(url) ~= "string" then return nil end
    local host = url:match("^%a[%w+.-]*://([^/?#]+)")
    if not host then return nil end
    host = host:gsub("^.*@", "")       -- drop userinfo
    host = host:gsub(":%d+$", "")      -- drop port
    return host:lower()
end
ABSClient._urlHost = url_host

--[[--
Add the Access headers to `headers` when credentials are configured and
`url` points at the configured ABS host (never leak them to other hosts).
--]]
function ABSClient:_applyAccessHeaders(headers, url)
    headers["CF-Access-Client-Id"] = nil
    headers["CF-Access-Client-Secret"] = nil
    if not self:hasAccessCredentials() then return headers end
    local target = url_host(url)
    if target and target == url_host(self.server_url) then
        headers["CF-Access-Client-Id"] = self.cf_access_client_id
        headers["CF-Access-Client-Secret"] = self.cf_access_client_secret
    end
    return headers
end

--[[--
Inspect a redirect target before following it.
@return string|nil  error message when the redirect must not be followed
--]]
function ABSClient:_checkRedirect(location, headers)
    local host = url_host(location)
    if host and host:match("cloudflareaccess%.com$") then
        if self:hasAccessCredentials() then
            return _("Blocked by Cloudflare Access: the service token was rejected. Check the Access Client ID/Secret and that the token is allowed on this application.")
        end
        return _("This server is behind Cloudflare Access. Set an Access service token under Audiobookshelf > Cloudflare Access.")
    end
    if host and host ~= url_host(self.server_url) then
        -- Cross-host redirect: do not forward credentials.
        headers["Authorization"] = nil
        headers["CF-Access-Client-Id"] = nil
        headers["CF-Access-Client-Secret"] = nil
    end
    return nil
end

--[[--
Quick connectivity check that works without an ABS login: GET /ping
(unauthenticated in ABS, but still gated by Cloudflare Access).
@return boolean, string|nil
--]]
function ABSClient:pingServer()
    local data, err = self:_request("/ping")
    if not data then return false, err end
    if type(data) == "table" and (data.success == true or data.raw) then
        return true, nil
    end
    return false, _("Unexpected response from /ping.")
end

-- ---------------------------------------------------------------------------
-- Internal: generic authenticated request
-- ---------------------------------------------------------------------------

--[[--
Perform an HTTP request to the ABS server.
@param endpoint string  API endpoint (e.g. "/api/libraries")
@param method string    HTTP method (default "GET")
@param body table       Lua table to JSON-encode as request body (optional)
@return table|nil, string|nil  response_data, error_message
--]]
function ABSClient:_request(endpoint, method, body)
    method = method or "GET"

    if not self.server_url or self.server_url == "" then
        return nil, _("No Audiobookshelf server configured.")
    end

    local http = require("socket.http")
    local ltn12 = require("ltn12")
    local socketutil = require("socketutil")
    local JSON = require("json")

    local url = self.server_url .. endpoint
    local sink = {}
    local headers = {
        ["Content-Type"] = "application/json",
        ["User-Agent"] = "audiobook.koplugin-absclient",
    }

    -- Add Bearer token if available
    if self.token and self.token ~= "" then
        headers["Authorization"] = "Bearer " .. self.token
    end
    self:_applyAccessHeaders(headers, url)  -- [cf-access patch]

    local request_body = nil
    if body then
        local ok, json_body = pcall(JSON.encode, body)
        if ok then
            request_body = json_body
            headers["Content-Length"] = tostring(#request_body)
        else
            return nil, _("Failed to encode request body.")
        end
    end

    socketutil:set_timeout(socketutil.FILE_BLOCK_TIMEOUT, socketutil.FILE_BLOCK_TIMEOUT)

    local code, response_headers, status = require("socket").skip(1, http.request{
        url = url,
        method = method,
        headers = headers,
        source = request_body and ltn12.source.string(request_body) or nil,
        sink = ltn12.sink.table(sink),
        redirect = false,  -- handle manually to preserve auth headers
    })

    socketutil:reset_timeout()

    -- If the request failed, code is a string error message (not a number).
    -- Guard here before any numeric comparisons.
    if code and type(code) ~= "number" then
        return nil, _("Network error: ") .. tostring(code)
    end

    -- Handle redirects manually (socket.http drops headers on redirect)
    if code and code >= 300 and code < 400 and response_headers and response_headers.location then
        local redirect_url = response_headers.location
        -- Resolve relative redirects
        if redirect_url:sub(1, 1) == "/" then
            local base = self.server_url:match("^(https?://[^/]+)")
            redirect_url = base .. redirect_url
        elseif not redirect_url:match("^https?://") then
            local base = self.server_url:match("^(https?://.+/)") or (self.server_url .. "/")
            redirect_url = base .. redirect_url
        end
        -- [cf-access patch] refuse Access login redirects; scope headers to ABS host
        local access_err = self:_checkRedirect(redirect_url, headers)
        if access_err then
            return nil, access_err
        end
        self:_applyAccessHeaders(headers, redirect_url)

        sink = {}
        socketutil:set_timeout(socketutil.FILE_BLOCK_TIMEOUT, socketutil.FILE_BLOCK_TIMEOUT)
        code, response_headers, status = require("socket").skip(1, http.request{
            url = redirect_url,
            method = method,
            headers = headers,
            source = request_body and ltn12.source.string(request_body) or nil,
            sink = ltn12.sink.table(sink),
            -- [cf-access patch] don't let socket.http auto-follow (and re-send
            -- the Access secret) on a second redirect when a token is set
            redirect = self:_followRedirectFlag(),
        })
        socketutil:reset_timeout()
    end

    if not code then
        return nil, _("Network error: no response from server.")
    end

    if type(code) ~= "number" then
        return nil, _("Network error: ") .. tostring(code)
    end

    if code == 401 then
        return nil, _("Authentication failed. Please log in again.")
    end

    if code == 404 then
        return nil, _("Not found (HTTP 404).")
    end

    if code >= 400 then
        return nil, _("HTTP error: ") .. tostring(code) .. " " .. tostring(status or "")
    end

    if code ~= 200 then
        return nil, _("Unexpected HTTP response: ") .. tostring(code)
    end

    local response_body = table.concat(sink)
    if not response_body or response_body == "" then
        return {}, nil
    end

    -- Try JSON decode; fall back to raw body wrapper for plain-text responses
    -- like "OK" from /api/session/local.
    local ok, data = pcall(JSON.decode, response_body)
    if ok then
        return data, nil
    elseif response_body:match("^%s*[%[{]") then
        -- Looks like JSON but failed to parse → real error
        return nil, _("Failed to parse server response.")
    else
        -- Plain text response (e.g., "OK")
        return {raw = response_body}, nil
    end
end

-- ---------------------------------------------------------------------------
-- Authentication
-- ---------------------------------------------------------------------------

--[[--
Log in to the ABS server.
@param username string
@param password string
@return string|nil, table|nil, string|nil  token, user, error_message
--]]
function ABSClient:login(username, password)
    if not username or username == "" or not password or password == "" then
        return nil, nil, _("Username and password are required.")
    end

    local data, err = self:_request("/login", "POST", {
        username = username,
        password = password,
    })

    if not data then
        return nil, nil, err
    end

    if data.error then
        return nil, nil, data.error
    end

    local token = data.token or data.user and data.user.token
    if not token then
        return nil, nil, _("Login succeeded but no token was returned.")
    end

    self.token = token
    self.user_id = data.user and data.user.id or ""

    return token, data.user, nil
end

-- ---------------------------------------------------------------------------
-- Libraries
-- ---------------------------------------------------------------------------

--[[--
Get all accessible libraries.
@return table|nil, string|nil  libraries_array, error_message
--]]
function ABSClient:getLibraries()
    local data, err = self:_request("/api/libraries")
    if not data then
        return nil, err
    end
    if type(data.libraries) == "table" then
        return data.libraries, nil
    end
    -- Some ABS versions return the array directly
    if type(data) == "table" and #data > 0 then
        return data, nil
    end
    return {}, nil
end

-- ---------------------------------------------------------------------------
-- Items
-- ---------------------------------------------------------------------------

--[[--
Get items in a library.
@param library_id string
@param limit number  (default 20)
@param page number   (default 0)
@return table|nil, string|nil  {results, total}, error_message
--]]
function ABSClient:getLibraryItems(library_id, limit, page)
    limit = limit or 20
    page = page or 0
    local endpoint = string.format("/api/libraries/%s/items?limit=%d&page=%d&sort=media.metadata.title", library_id, limit, page)
    local data, err = self:_request(endpoint)
    if not data then
        return nil, err
    end
    return data, nil
end

--[[--
Fetch every item in a library in a single request (minified payloads).

Used for client-side search.  The server's GET /api/libraries/{id}/search
endpoint is a quick-search capped at 12 relevance-ordered matches with no
offset, which silently truncated large libraries (issue #65); the items
endpoint accepts an arbitrarily large limit and returns every title,
so the plugin fetches the whole list once and filters locally.

@param library_id string
@param max_items number  safety cap for enormous libraries (default 10000)
@return table|nil, string|nil  {results, total}, error_message
--]]
function ABSClient:getAllLibraryItems(library_id, max_items)
    max_items = max_items or 10000
    local endpoint = string.format(
        "/api/libraries/%s/items?limit=%d&page=0&sort=media.metadata.title&minified=1",
        library_id, max_items)
    local data, err = self:_request(endpoint)
    if not data then
        return nil, err
    end
    return data, nil
end

--[[--
Get detailed item information.
@param item_id string
@return table|nil, string|nil  item_details, error_message
--]]
function ABSClient:getItemDetails(item_id)
    local data, err = self:_request("/api/items/" .. item_id .. "?expanded=1")
    if not data then
        return nil, err
    end
    return data, nil
end

--[[--
Get the full URL for an item's cover image.
@param item_id string
@return string
--]]
function ABSClient:getCoverUrl(item_id)
    return self.server_url .. "/api/items/" .. item_id .. "/cover"
end

-- ---------------------------------------------------------------------------
-- Progress
-- ---------------------------------------------------------------------------

--[[--
Get playback progress for a specific item.
@param item_id string
@return table|nil, string|nil  progress, error_message
--]]
function ABSClient:getProgress(item_id)
    local data, err = self:_request("/api/me/progress/" .. item_id)
    if not data then
        return nil, err
    end
    return data, nil
end

--[[--
Update playback progress for an item.
Uses POST /api/session/local which is the correct endpoint for v2.35.1+.
@param item_id string
@param current_time number  Current position in seconds
@param duration number      Total duration in seconds
@param is_finished boolean  Whether playback is complete
@return boolean, string|nil  success, error_message
--]]
function ABSClient:updateProgress(item_id, current_time, duration, is_finished)
    current_time = current_time or 0
    duration = duration or 0

    local body = {
        libraryItemId = item_id,
        currentTime = current_time,
        duration = duration,
        isFinished = is_finished or false,
        timeListened = 0,  -- server calculates this from sessions
    }

    local data, err = self:_request("/api/session/local", "POST", body)
    if not data then
        return false, err
    end
    return true, nil
end

--[[--
Batch update progress for multiple items.
Uses POST /api/session/local-all for efficient bulk sync.
@param updates table  Array of {libraryItemId, currentTime, duration, isFinished}
@return boolean, string|nil  success, error_message
--]]
function ABSClient:batchSyncProgress(updates)
    if not updates or #updates == 0 then
        return true, nil
    end

    local sessions = {}
    for _, u in ipairs(updates) do
        table.insert(sessions, {
            libraryItemId = u.libraryItemId,
            currentTime = u.currentTime or 0,
            duration = u.duration or 0,
            isFinished = u.isFinished or false,
            timeListened = 0,
        })
    end

    local data, err = self:_request("/api/session/local-all", "POST", sessions)
    if not data then
        return false, err
    end
    return true, nil
end

-- ---------------------------------------------------------------------------
-- File download
-- ---------------------------------------------------------------------------

--[[--
Download a file from the ABS server to a local path.
Uses socket.http so no external curl/wget is required.
@param url string  Full URL (may be an API endpoint or content URL)
@param dest_path string  Local filesystem path to write
@return boolean, string|nil  success, error_message
--]]
function ABSClient:downloadFile(url, dest_path)
    if not url or url == "" then
        return false, _("No download URL provided.")
    end

    local http = require("socket.http")
    local ltn12 = require("ltn12")
    local socketutil = require("socketutil")

    local headers = {
        ["User-Agent"] = "audiobook.koplugin-absclient",
    }
    if self.token and self.token ~= "" then
        headers["Authorization"] = "Bearer " .. self.token
    end
    self:_applyAccessHeaders(headers, url)  -- [cf-access patch]

    -- Ensure parent directory exists
    local dir = dest_path:match("^(.*)/[^/]*$")
    if dir and dir ~= "" then
        os.execute(string.format('mkdir -p "%s"', dir))
    end

    local f, ferr = io.open(dest_path, "wb")
    if not f then
        return false, _("Cannot open destination file: ") .. tostring(ferr)
    end

    socketutil:set_timeout(socketutil.FILE_BLOCK_TIMEOUT, socketutil.FILE_BLOCK_TIMEOUT)

    local code, response_headers, status = require("socket").skip(1, http.request{
        url = url,
        method = "GET",
        headers = headers,
        sink = ltn12.sink.file(f),
        redirect = false,
    })

    -- Guard: if the request failed, code is a string error message.
    -- Check this before any numeric comparison to avoid crashing.
    if code and type(code) ~= "number" then
        socketutil:reset_timeout()
        return false, _("Network error: ") .. tostring(code)
    end

    -- Handle redirects manually (socket.http drops headers on redirect)
    if code and code >= 300 and code < 400 and response_headers and response_headers.location then
        local redirect_url = response_headers.location
        if redirect_url:sub(1, 1) == "/" then
            local base = self.server_url:match("^(https?://[^/]+)")
            redirect_url = base .. redirect_url
        elseif not redirect_url:match("^https?://") then
            local base = self.server_url:match("^(https?://.+/)") or (self.server_url .. "/")
            redirect_url = base .. redirect_url
        end
        -- [cf-access patch] refuse Access login redirects; scope headers to ABS host
        local access_err = self:_checkRedirect(redirect_url, headers)
        if access_err then
            socketutil:reset_timeout()
            os.remove(dest_path)
            return false, access_err
        end
        self:_applyAccessHeaders(headers, redirect_url)
        f, ferr = io.open(dest_path, "wb")
        if not f then
            socketutil:reset_timeout()
            return false, _("Cannot reopen destination file: ") .. tostring(ferr)
        end
        socketutil:set_timeout(socketutil.FILE_BLOCK_TIMEOUT, socketutil.FILE_BLOCK_TIMEOUT)
        code, response_headers, status = require("socket").skip(1, http.request{
            url = redirect_url,
            method = "GET",
            headers = headers,
            sink = ltn12.sink.file(f),
            redirect = self:_followRedirectFlag(),  -- [cf-access patch]
        })
    end

    socketutil:reset_timeout()

    if not code then
        return false, _("Network error: no response from server.")
    end
    if type(code) ~= "number" then
        return false, _("Network error: ") .. tostring(code)
    end
    if code == 401 then
        return false, _("Authentication failed.")
    end
    if code >= 400 then
        return false, _("HTTP error: ") .. tostring(code)
    end
    if code ~= 200 then
        return false, _("Unexpected HTTP response: ") .. tostring(code)
    end

    return true, nil
end

-- ---------------------------------------------------------------------------
-- Server health / connectivity check
-- ---------------------------------------------------------------------------

--[[--
Check if the configured server is reachable.
@return boolean, string|nil  is_reachable, error_message
--]]
function ABSClient:ping()
    local data, err = self:_request("/api/libraries")
    if not data then
        return false, err
    end
    return true, nil
end

return ABSClient
