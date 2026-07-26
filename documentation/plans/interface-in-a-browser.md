---
created: 2026-07-26T18:40:00Z
updated: 2026-07-26T18:40:00Z
commit: 95cb0b8
---

# The Interface Should Not Require a Window

The desktop app is a Tauri shell around a static site that talks to the daemon over loopback HTTP. Nothing about that site needs a window. It is a folder of HTML, JavaScript and images, produced by `bun run build` and mounted by the shell — and yet the only way to look at a Daisy session in anything but a terminal was to install a macOS application. On a headless machine there was no interface at all; over an SSH tunnel there was one only if you were willing to install an app locally and point it across; on a machine where you simply did not want an application, there was nothing.

That is a strange restriction for a harness whose whole argument is that a session is an addressable process. The daemon already serves its control plane on loopback precisely because a webview cannot open a unix socket — which means it already serves it in the one form a browser can consume. The missing piece was never the API. It was somewhere to get the page from.

`daisy web` is that. It serves the same export the app embeds, and the command line is the right place for it: the command line owns the daemon, so it can bring one up before serving a page that would otherwise be a spinner over nothing.

## Proxy, not a pointer

Serving files would have been twenty lines. The interesting decision is what the page then talks to, and the obvious answer is wrong.

Pointing the browser directly at the daemon means the page needs two things it does not have. It needs the daemon's port, which is ephemeral and chosen at every boot, so the bundle's build-time default of `127.0.0.1:8823` is a port nothing is listening on. And it needs the capability token, which authorises full control of the daemon — every tool, every session, every file the harness can reach. Handing that to a page means handing it to a browser: to its storage, which outlives the tab, and to whatever extensions are loaded. The desktop webview already holds that token, but a webview is a private, single-purpose browser; a real one is not.

So this proxies instead. The interface is served at the root, and everything that is not a file is forwarded to the daemon with the token attached here, in this process, where it already was. The browser talks only to this server's own origin. Three things fall out of that at once: the token never reaches the page, the ephemeral port is nobody's business but this file's, and CORS does not arise because there is only one origin in play.

What is proxied is everything, not the easy part. Ordinary requests, the server-sent event stream that carries a session's transcript, and the two websockets — the terminal and the artifact preview's own relay. A proxy that handled only the first would look like it worked and then fail at exactly the moment somebody tried to use it.

## One handler, because two routes cannot work

The first attempt mounted a static handler at the root and appended a catch-all proxy route, which produced an interface that 404ed. An ASGI router matches in order and a mount at `/` answers every path, so whichever of the two comes first swallows the other: the catch-all proxies the interface to a daemon that does not have it, and the mount 404s every daemon path it does not have. There is no ordering of two routes that serves both.

One handler that looks before it forwards is the only arrangement that works. A `GET` or `HEAD` whose path resolves to a real file inside the export is served from disk; everything else is the daemon's. Resolution is done with `resolve()` and then checked to be inside the export root, so `..` in a URL cannot climb out of it.

## How the page knows

The bundle resolves its API base through a short chain, and this adds one link. In a browser it fetches `/__daisy/runtime.json`; a server that answers it is this one, and the answer is an empty base — address the daemon relative to this origin. Any other static host does not answer, the fetch fails harmlessly, and the existing build-time default applies exactly as before. One consequence needed fixing in the same place: a websocket URL must be absolute, and an empty base made `new URL(path, "/")` throw, so the page's own origin stands in.

## Shipped, not just built

The freeze now carries the export, about fifteen megabytes against an image of two hundred and thirty, and the freshness guard watches `web/out` like every other input. Without it `daisy web` would work from a checkout and fail from the thing people actually install, which is the wrong way round for a feature whose reason to exist is machines you have not set up for development. When the interface has not been built the freeze still succeeds and says so, and the command explains what to run.

## The thing to be careful about

This server holds the daemon's token, so whatever can reach its address can drive the daemon. It binds loopback for that reason and says so on startup. `--host` exists because tunnelling to a remote daemon is a real use, and anyone who reaches for it is choosing to put something in front of it.
