# The phone

Frank's daemon is deliberately unreachable. It binds loopback on a port it picks fresh every boot and mints a capability token to match, so nothing off the machine can address it. That is the right default, and the mobile client does not change it — it adds a second front door, with a lock on it.

Three pieces. **`frank reach`** is a proxy on the machine, on a loopback port that stays the same, behind a token that survives a reboot. **Tailscale** is what carries it off the machine — a stable name, a real TLS certificate, and WireGuard between your own devices. **The app** is an Expo client in `mobile/` that pairs with that door once and then shows you the interface.

The machine has to be awake and the daemon running for any of it to work. A phone is a client, not a second Frank.

## There is one interface, and the phone runs it

Everything you see on the phone — the sessions list, the transcript, the tool rows and their shimmer, the composer, the approval cards, the settings — is `web/`, the same bundle the Tauri app puts in a window and `frank serve` hands a browser. The app is a WebView around it. Frank's desktop app is already a webview around that bundle; this is the same arrangement on a device that happens to be a phone.

It began as a React Native port of those screens, and the reason it is not one any more is worth recording. A port can be faithful on the day it is written and cannot *stay* faithful, because nothing structurally prevents it drifting — and it drifted, in ways obvious to anyone who used both: a "thinking" row the desktop deliberately does not render, a spinner where the desktop shimmers, a workspace named `giovannigravili +1` where the desktop says `giovannigravili, colima`. Each was a decision made a second time because reaching the first one was inconvenient. None of them is reachable now, because there is nowhere for a second implementation to live.

The consequence is the point and it is not free: **making the interface good on a phone is now work on `web/`.** That is where it belongs — a dialog that is unusable at 390pt is unusable in a narrow browser window too.

## What the phone can do

The conversation, whole: read a transcript, start a session, send a message, steer a running turn, answer an approval or a question, stop a turn, fold up context, end a session. Dictation, terminals, schedules, settings — everything the desktop has, because it *is* the desktop's interface.

## Running it

### Once

Build the interface. `frank reach` serves this directory, and without it you get the control plane and no screens.

```bash
cd web && bun run build
```

Install the app's dependencies.

```bash
cd mobile && bun install
```

Install **Expo Go** from the App Store on the phone. Everything the app uses — the camera for pairing, and the keychain — is in Expo Go, so no native build and no Xcode is needed.

Set Tailscale up on both devices, once. The four steps are under [Setting Tailscale up](#setting-tailscale-up-once) below; `frank reach` refuses to start until they are done, and says which one is missing.

### Every time

Two servers, each in its own terminal, plus the daemon, which `frank reach` starts if it is not already up.

Start the door the phone comes in by. It prints its pairing link and then serves.

```bash
frank reach
```

Start the bundler that delivers the app to Expo Go. It prints its own QR.

```bash
cd mobile && bun run start
```

Then, on the phone: scan the **Expo** QR to load the app, and once it opens on its pairing screen, paste the link `frank reach` printed. `frank reach pair` prints it on its own, one line on stdout, so it pipes:

```bash
frank reach pair | pbcopy
```

### When the QR does not print

Expo only draws its QR when its output is a terminal, so a run that is redirected to a file or launched in the background shows nothing. The URL is all Expo Go needs, and you can type it in by hand:

```bash
echo "exp://$(ipconfig getifaddr en0):8081"
```

`frank reach pair` reprints its own code at any time, without starting a second server:

```bash
frank reach pair
```

### Iterating on the interface

`frank reach` serves `web/out`, so a change to `web/src` needs `bun run build` — forty seconds, whether the change was a component or a colour. That is a poor loop for the thing most likely to need fixing, so it can serve a **dev server** instead, and then a change reaches the phone the moment it is saved:

```bash
cd web && FRANK_PROXY_ENABLED=1 bunx next dev --webpack
```

```bash
frank reach --interface
```

Two things about that command are load-bearing. `FRANK_PROXY_ENABLED` empties `assetPrefix`, which in dev is otherwise an absolute `http://localhost:3000` — a machine the phone holding the page does not have. And `--webpack` rather than the default Turbopack, because Turbopack's dev server bundles for Node and then cannot resolve what `bun install` laid out: it asks for `@swc/helpers-<hash>`, which is bun's deduplication naming and not a directory that exists. Nothing in this repository causes that and nothing here can fix it; webpack resolves the same tree without complaint.

Hot reload reaches the phone because reach relays the bundler's websocket too. The daemon still answers everything that is not the interface, and the reach token is still required — the dev server is handed no credential, because it is not the daemon.

### Checking before you blame the phone

Metro is the one thing the phone still reaches over the LAN, so it needs the same Wi-Fi and an address that is not loopback:

```bash
curl -s -o /dev/null -w "%{http_code}\n" "http://$(ipconfig getifaddr en0):8081/status"
```

Frank itself does not, and this is the check for it — it asks the loopback listener directly, which is the only interface it binds. A 200 means the build exists and the token works:

```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "Sec-Fetch-Dest: document" "http://127.0.0.1:8825/?token=$(cat ~/.local/share/frank/reach-token)"
```

## Making the machine reachable

Over Tailscale, and only over Tailscale. `frank reach` binds `127.0.0.1` and never anything else; what faces the network is `tailscale serve`, which puts a listener on your tailnet, terminates TLS with a Let's Encrypt certificate for this machine's `*.ts.net` name, and proxies to that loopback port. Every request still needs the reach token, and anything without it gets a 401 without touching the daemon — websocket handshakes included.

```bash
frank reach
```

That starts the daemon if it is not running, configures `tailscale serve`, and prints the pairing link.

The serve configuration outlives the command, deliberately. It costs nothing while `frank reach` is not running — the address answers with a connection error, which a phone shows as "not answering" either way — and re-asserting it on every start is cheaper and more reliable than a teardown that could only ever be best-effort. `tailscale serve --https=443 off` removes it.

| Flag | What it does |
|---|---|
| `-p`, `--port` | The loopback port Tailscale proxies to. Default 8825. Nothing listens on a network interface, so this only matters if something else already has the port. |
| `--interface` | Serve the interface from a running dev server instead of the built export. |

`frank reach pair` prints the pairing code without starting a server. `frank reach rotate` mints a new token, which unpairs every device holding the old one.

### Why a tailnet rather than the LAN

The first two reasons are the ones you would expect. The address is stable — `mac.tailnet.ts.net` is the machine's name for as long as it is on the tailnet, where a LAN address is a DHCP lease that changes and leaves a paired phone asking for a number nobody answers to. And nothing is exposed: no port is open on the LAN, nothing is forwarded at the router, and WireGuard carries the traffic between authenticated devices.

The third is the one that actually forced it. **A page served over plain HTTP to anything but `localhost` is not a secure context**, and browsers withhold a growing list of APIs from those pages — `navigator.mediaDevices`, `navigator.clipboard`, `crypto.randomUUID`. The interface is a real web application that uses all three. Served from `http://192.168.1.30:8825` it does not degrade gracefully; it breaks one API at a time, with errors that read as faults in Frank: dictation that says there is no microphone, a paste button that throws, a message that will not send. Guarding each one as it turns up is whack-a-mole with a list that browsers keep extending. Being a secure origin ends the entire class.

`tailscale funnel` would put this on the public internet and is deliberately not used. The token is a bearer credential with full control of the machine, which is exactly what [`SECURITY.md`](../SECURITY.md) says to tunnel rather than expose.

### What the daemon knows about any of this

Nothing, and that is deliberate. Three layers, each reaching exactly as far as how it binds:

```
tailscale serve   the tailnet          TLS, a real certificate, WireGuard
  → frank reach   127.0.0.1:8825       the reach token, durable across reboots
    → frankd      127.0.0.1:<random>   a capability token, new every boot
```

No layer is configured to be safe; each one *is* safe because of what it binds to. There is no flag that makes `frank reach` listen on a network interface, and no setting in the daemon that mentions Tailscale.

### Setting Tailscale up, once

1. Install it. On this machine that is `~/.config/nix-darwin` → `homebrew.casks` → `tailscale-app`, then `rebuild`. The standalone variant rather than the App Store one: Tailscale recommends it, and it is the one that carries the full CLI. Never run both.
2. Sign in from the Tailscale app, and install the phone's client from the App Store and sign in with the same account.
3. Turn on three things for the tailnet, once each, **in this order**. **MagicDNS**, under [DNS](https://login.tailscale.com/admin/dns) — without it the machine's name resolves nowhere. Then **HTTPS Certificates**, on the same page and only offered once MagicDNS is on; without it Tailscale will not issue a certificate for that name, and `tailscale serve` happily listens on 443 with nothing to present. Then **Serve**, which `frank reach` hands you a one-click link for the first time it needs it — Tailscale prints that link and then waits, polling, for somebody to follow it.

The order is the part worth knowing: with MagicDNS off, the HTTPS switch does nothing, so it is possible to believe both are on while neither is. `frank reach` checks MagicDNS itself and stops rather than announcing an address that cannot serve TLS.
4. In the Mac app's settings, switch on the CLI integration, which puts `tailscale` on `PATH`. Frank also looks inside `/Applications/Tailscale.app` if it is not there.

### The token, and how it gets into the page

The reach token is minted once, kept in `~/.local/share/frank/reach-token` at mode 0600, and unaffected by restarts — unlike the daemon's own capability token, which is new on every boot and would unpair a device every time the machine woke up. It carries full control of the daemon: the link is meant for a phone, not for a room.

The app opens `https://endpoint/?token=…` exactly once. `frank reach` answers that document with an `HttpOnly` session cookie, and every script, font, event stream and websocket the page asks for afterwards carries the token without the page ever holding it. A page cannot attach a credential to its own subresources; a cookie is attached by the transport, and no script can read it back out. The cookie is stripped before anything reaches the daemon, as the header and query forms are.

## Looking at it without a phone

The interface is a web page, so the mobile layout is a browser window at the right width — no device, simulator or build. Two tools, answering different questions.

**Chrome DevTools device mode** (⌘⇧M) for quick layout work: device presets, pixel ratio, touch emulation, throttling. Its one gap is the safe areas — Chrome reports `env(safe-area-inset-*)` as `0px` whatever device you pick, and has since the request was [filed in 2020](https://issues.chromium.org/issues/40718410). That does not matter here, because `globals.css` never calls `env()` where the layout reads it. Every inset is mapped once onto a custom property and consumed as `var(--safe-top)`, so simulating a notch is two values typed into the Styles pane on `:root`:

```css
--safe-top: 59px;    /* iPhone 15 */
--safe-bottom: 34px;
```

**Safari for the truth.** The phone app is a `WKWebView`, which is WebKit; Chrome is Blink, so a layout checked only in Chrome is a layout checked in the wrong engine. Turn on Settings → Advanced → "Show features for web developers", then ⌥⌘R for Responsive Design Mode — and better, plug the phone in and use Develop → *your iPhone* → the webview, which inspects the real page on the real device with real insets and needs no Xcode. That is the check worth trusting.

## When something is wrong

| What you see | What it usually is |
|---|---|
| Expo Go cannot reach the dev server | The phone is on a different network, or Metro bound to loopback. Check the `/status` probe above. |
| The app opens on the pairing screen every launch | The pairing did not persist. On a device that is the keychain; in a browser it is `localStorage`. |
| "not answering" on the pairing's address | `frank reach` is not running, the Mac is asleep, or one of the two devices is off the tailnet. The address itself does not go stale, so pairing again fixes nothing — check `tailscale status` on both. |
| Paired, but the screens never load | The interface was not built. `cd web && bun run build`, then reload. |
| Everything 401s | The token was rotated. `frank reach pair` and scan again. |
| The mic button does nothing | `getUserMedia` inside the webview. The permission plumbing is in `mobile/app.json` and `mobile/src/app/index.tsx`; iOS asks once, and a refusal is remembered. |
| "only opens a microphone over a secure connection" | The page was reached at something other than the tailnet address — a LAN address or `127.0.0.1` from another device. Use the `*.ts.net` one; on this Mac, `http://localhost:8825` also counts as secure. |
| `frank reach` says Tailscale is not connected | Open the Tailscale app and sign in. It says exactly which of the four setup steps above is missing. |
| Expo Go says to run `eas init` | Ignore it. EAS is the cloud build service; this project has no EAS configuration and local development needs none. |
| Expo Go's server list is empty | Discovery is mDNS, which on iOS needs Local Network permission — Settings → Expo Go → Local Network. Not needed if you open the `exp://` URL directly. |
| The pairing screen will not open the camera | It asks on arrival now. If it was refused once, iOS will not ask again — the screen offers Settings, or use the Paste link tab. |

## How it is put together

```
mobile/src
  app/
    index.tsx        a WebView onto the machine's own interface
    pair.tsx         the camera, and the token going into the keychain
  lib/connection.tsx whether the machine is answering, and holding the pairing
  lib/intl.tsx       the same message catalogue the desktop reads
  theme/             tokens, for the two screens above and nothing else
```

That is the whole application. Everything else is `web/src`.

What the two clients share beyond that lives in [`shared/`](../shared/README.md) and is read by the desktop too: the message catalogue, the wire event union, workspace naming, status colours, and the tool glyph vocabulary.

### Dictation

There is nothing here about it, and that is the point. The page calls `getUserMedia` and records through its own `AudioWorklet`, exactly as it does on the desktop — because over Tailscale the page is a secure context and the browser hands it a microphone.

It was briefly otherwise. Over plain HTTP the microphone was closed to the page, so the shell recorded on its behalf and posted the samples to the daemon itself: a second recording implementation, in a second language, of something already implemented. That is the same drift the WebView exists to prevent, bought back in a different currency. Making the origin secure deleted it.

## What has been done for narrow screens, and what has not

**Dialogs** are full-bleed below `sm` — square corners, full height, a header and footer that stay put while the body scrolls between them. Stated once in the dialog slot recipe in `web/src/components/ui/provider.tsx` so every dialog inherits it; the settings dialog, the model picker and the attachment lightbox carry their own widths and were made responsive to match.

**Safe areas.** `viewport-fit=cover` in the root layout is what makes `env(safe-area-inset-*)` report anything at all. `--app-inset-top` in `globals.css` combines the notch with the Tauri titlebar and the shell reserves it once; the composer, the sidebar and the side panels each reserve the bottom so nothing sits under the home indicator. The phone app deliberately adds no padding of its own — the page owns its insets, and two layers reserving the same strip is a black band at the top.

**Touch targets.** The house control height is 32px, right for a pointer and under both Apple's and Google's floor for a finger. Rather than a second set of sizes threaded through every call site, controls grow to 40px under `@media (pointer: coarse)` — the exact condition that makes 32px wrong, leaving a mouse-driven window untouched. Dropdown rows grow with them.

**The terminal** gets larger cells and touch-rate scrolling on a coarse pointer: a 12px cell is below what a thumb can place a cursor in, and momentum scrolling moves far more rows per gesture than a wheel notch does.

Still to do, and unverified because it needs hardware:

- The whole thing on a real device, in WebKit. Everything above is reasoned from the code and checked in a build; what has been looked at was looked at in Chrome, which is the wrong engine for a `WKWebView` target.
- The composer's control row wraps below 460px and orphans its last chip on a second line. That is the desktop's own responsive ladder bottoming out, and it wants another breakpoint.
- Long-press and swipe affordances exist nowhere. Deleting a session is a `⋯` menu designed for a hover.
- Dictation through the native bridge, end to end on a device — the recording path is written and typechecked but has never had a real microphone on it.
- Whether the pairing cookie survives a cold start of the app.

## Checks

```bash
cd mobile && bunx tsc --noEmit && bunx expo lint
```

The reach listener's guard and the daemon's own token check are exercised ad hoc against a running daemon rather than kept as test files: that nothing without the token gets through, HTTP or websocket, that the cookie exchange happens only for documents, and that neither the token nor the cookie is forwarded to the daemon.
