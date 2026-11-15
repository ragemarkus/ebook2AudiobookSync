# Cloudflare Access patch (local fork)

This is an unofficial patch of **Audiobook Read-Along for KOReader** v0.6.13
by stradichenko — https://github.com/stradichenko/audiobook.koplugin
(GNU AGPL-3.0; see LICENSE). All credit for the plugin goes to the original author.

What the patch changes (only `absclient.lua`, `absbrowse.lua`, plus the new `abs_access.example.lua`):

* Every Audiobookshelf request (API calls, login, progress sync, cover and audio
  downloads) carries `CF-Access-Client-Id` and `CF-Access-Client-Secret`
  when a Cloudflare Access service token is configured.
* The headers go only to the configured Audiobookshelf host. On a redirect to a
  different host, the Access headers and the ABS Bearer token are stripped.
* A redirect to `*.cloudflareaccess.com` (the Access login page) is reported as a
  clear error instead of being parsed as a server reply.
* With a token set, socket.http does not auto-follow a second redirect.
* New menu item: Tools > Audiobook Read-Along > Audiobookshelf > Cloudflare Access,
  with Save & test (GET /ping through Access) and Clear.
* Fallback config file: `abs_access.lua` in the plugin folder.
* If either value is blank, the plugin behaves exactly like upstream.

Note: "Check for updates" in the plugin installs the upstream release and will
remove this patch.
