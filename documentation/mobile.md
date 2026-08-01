# The phone

Frank's daemon is deliberately unreachable. It binds loopback on a port it picks fresh every boot and mints a capability token to match, so nothing off the machine can address it. That is the right default, and the mobile client does not change it — it adds a second front door, with a lock on it.

Two pieces. **`frank reach`** is a proxy on the machine: a port that stays the same, behind a token that survives a reboot. **The app** is an Expo client in `mobile/` that pairs with that door once and then shows you the interface.

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

Install **Expo Go** from the App Store on the phone. Everything the app uses — the camera for pairing, the keychain, the microphone — is in Expo Go, so no native build and no Xcode is needed.

### Every time

Two servers, each in its own terminal, plus the daemon, which `frank reach` starts if it is not already up.

Start the door the phone comes in by. It prints its pairing QR and then serves.

```bash
frank reach
```

Start the bundler that delivers the app to Expo Go. It prints its own QR.

```bash
cd mobile && bun run start
```

Then, on the phone: scan the **Expo** QR to load the app, and once it opens on its pairing screen, scan the **`frank reach`** QR to point it at your machine. Two codes, in that order — one loads the app, the other tells it where Frank is.

A QR drawn in the terminal needs the right window size, font and colours to be scannable at all, and degrades to noise rather than to a smaller code when any of those is wrong. `--image` writes a PNG instead and opens it, which a camera reads without argument:

```bash
frank reach pair --image
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

### Checking before you blame the phone

The dev server has to be reachable from the phone, which means the same Wi-Fi and a LAN address rather than loopback:

```bash
curl -s -o /dev/null -w "%{http_code}\n" "http://$(ipconfig getifaddr en0):8081/status"
```

And the interface has to actually be served — a 200 here means the build exists and the token works:

```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "Sec-Fetch-Dest: document" "http://127.0.0.1:8825/?token=$(cat ~/.local/share/frank/reach-token)"
```

## Making the machine reachable

`frank reach` starts the daemon if it is not running, serves on `0.0.0.0:8825`, and prints a QR code. Every request needs the reach token; anything without it gets a 401 and never touches the daemon. Websocket handshakes are checked too.

| Flag | What it does |
|---|---|
| `-p`, `--port` | The port. Default 8825, and fixed on purpose — a phone cannot be told a new one every morning. |
| `--host` | What to bind. Default `0.0.0.0`, because a phone cannot reach loopback. |
| `--advertise` | The address to hand the phone when something else fronts this: a reverse proxy, or a tunnel. Takes a host or a whole URL. |
| `--tls-certificate`, `--tls-key` | Serve TLS directly. |

`frank reach pair` prints the pairing code without starting a server. `frank reach rotate` mints a new token, which unpairs every device holding the old one.

### Where it can be reached from

The pairing code carries a **list** of addresses, best first, and the app races them on every connect and keeps whichever answers. That is what makes the endpoint stable without anything having a fixed IP: at home the phone uses the LAN address, away it uses the tailnet one, and the connection does not notice.

The list is built from, in order: whatever `--advertise` said; this machine's Tailscale name, if it is on a tailnet; and the LAN address the routing table says faces the network.

**Tailscale is the recommended shape**, by some distance. The address is stable for the life of the machine, WireGuard carries the token, and nothing is listening on a public port anywhere. Install it on the Mac and on the phone and `frank reach` finds the address by itself.

A reverse proxy terminating TLS on a hostname you own is the same bargain differently bought — point it at `127.0.0.1:8825` and run `frank reach --host 127.0.0.1 --advertise https://frank.example.com`, and the phone gets a real certificate.

What is **not** supported is forwarding port 8825 on your router. It is a bearer token over plain HTTP; [`SECURITY.md`](../SECURITY.md) says to tunnel that rather than expose it, and this does not change that advice.

### The token, and how it gets into the page

The reach token is minted once, kept in `~/.local/share/frank/reach-token` at mode 0600, and unaffected by restarts — unlike the daemon's own capability token, which is new on every boot and would unpair a device every time the machine woke up. It carries full control of the daemon: the QR code is meant for a phone, not for a room.

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
| "not answering" on the pairing's addresses | `frank reach` is not running, or the phone left the network the LAN address belongs to. Pull to refresh, or `frank reach pair` again after joining a tailnet. |
| Paired, but the screens never load | The interface was not built. `cd web && bun run build`, then reload. |
| Everything 401s | The token was rotated. `frank reach pair` and scan again. |
| The mic button does nothing | `getUserMedia` inside the webview. The permission plumbing is in `mobile/app.json` and `mobile/src/app/index.tsx`; iOS asks once, and a refusal is remembered. |
| Expo Go says to run `eas init` | Ignore it. EAS is the cloud build service; this project has no EAS configuration and LAN development needs none. |
| Expo Go's server list is empty | Discovery is mDNS, which on iOS needs Local Network permission — Settings → Expo Go → Local Network. Not needed if you open the `exp://` URL directly. |
| The terminal QR will not scan | `frank reach pair --image`, which writes a PNG and opens it. |
| The pairing screen will not open the camera | It asks on arrival now. If it was refused once, iOS will not ask again — the screen offers Settings, or use the Paste link tab. |

## How it is put together

```
mobile/src
  app/
    index.tsx        a WebView onto the machine's own interface
    pair.tsx         the camera, and the token going into the keychain
  lib/connection.tsx which of the machine's addresses answers, and holding the pairing
  theme/             tokens, for the two screens above and nothing else
```

That is the whole application. Everything else is `web/src`.

What the two clients share beyond that lives in [`shared/`](../shared/README.md) and is read by the desktop too: the message catalogue, the wire event union, workspace naming, status colours, and the tool glyph vocabulary.

## What has been done for narrow screens, and what has not

**Dialogs** are full-bleed below `sm` — square corners, full height, a header and footer that stay put while the body scrolls between them. Stated once in the dialog slot recipe in `web/src/components/ui/provider.tsx` so every dialog inherits it; the settings dialog, the model picker and the attachment lightbox carry their own widths and were made responsive to match.

**Safe areas.** `viewport-fit=cover` in the root layout is what makes `env(safe-area-inset-*)` report anything at all. `--app-inset-top` in `globals.css` combines the notch with the Tauri titlebar and the shell reserves it once; the composer, the sidebar and the side panels each reserve the bottom so nothing sits under the home indicator. The phone app deliberately adds no padding of its own — the page owns its insets, and two layers reserving the same strip is a black band at the top.

**Touch targets.** The house control height is 32px, right for a pointer and under both Apple's and Google's floor for a finger. Rather than a second set of sizes threaded through every call site, controls grow to 40px under `@media (pointer: coarse)` — the exact condition that makes 32px wrong, leaving a mouse-driven window untouched. Dropdown rows grow with them.

**The terminal** gets larger cells and touch-rate scrolling on a coarse pointer: a 12px cell is below what a thumb can place a cursor in, and momentum scrolling moves far more rows per gesture than a wheel notch does.

Still to do, and unverified because it needs hardware:

- The whole thing on a real device, in WebKit. Everything above is reasoned from the code and checked in a build; what has been looked at was looked at in Chrome, which is the wrong engine for a `WKWebView` target.
- The composer's control row wraps below 460px and orphans its last chip on a second line. That is the desktop's own responsive ladder bottoming out, and it wants another breakpoint.
- Long-press and swipe affordances exist nowhere. Deleting a session is a `⋯` menu designed for a hover.
- `getUserMedia` for dictation inside a `WKWebView` — the permission plumbing is in place and has never been exercised.
- Whether the pairing cookie survives a cold start of the app.

## Checks

```bash
cd mobile && bunx tsc --noEmit && bunx expo lint
```

The reach listener's guard is tested in `tests/test_reach.py`: that nothing without the token gets through, HTTP or websocket, that the cookie exchange happens only for documents, and that neither the token nor the cookie is forwarded to the daemon. The daemon's own token check is tested in `tests/test_daemon_authentication.py`.
