-- [cf-access patch] Optional Cloudflare Access service token for Audiobookshelf.
-- Copy to abs_access.lua (same folder) and fill in, OR set it from
-- Tools > Audiobook Read-Along > Audiobookshelf > Cloudflare Access.
-- Values set in the menu take priority over this file.
-- Leave blank (or delete the file) if your server is not behind Cloudflare Access.
return {
    client_id = "",      -- e.g. "abcd1234....access"
    client_secret = "",
}
