# The phone

Frank's daemon is deliberately unreachable. It binds loopback on a port it picks fresh every boot
and mints a capability token to match, so nothing off the machine can address it. That is the
right default, and the mobile client does not change it — it adds a second front door, with a
lock on it.

Two pieces:

- **`frank reach`** — a proxy on the machine, on a port that stays the same, behind a token that
  survives a reboot.
- **The app** — an Expo client in `mobile/`, which pairs with that door once and then behaves
  like the desktop app, adapted for a phone.

The machine has to be awake and the daemon running for any of it to work. A phone is a client, not
a second Frank.

## What the phone can do

The conversation, whole: read a transcript, start a session, send a message, steer a running turn,
answer an approval or a question, stop a turn, fold up context, end a session. Dictation works —
you speak into the phone and the Mac transcribes it, on the Mac, with the model it already has.
The session list is the desktop's sidebar with the peer tree intact. Schedules can be read,
toggled, run and deleted.

What it deliberately does not do: terminals, file attachments, API keys, sandbox profiles, model
and provider configuration. Those are done in front of the thing they configure. Schedules are
read and toggled but not written, because nobody wants to type a cron expression on a phone.

## Making the machine reachable

```sh
frank reach
```

That starts the daemon if it is not running, serves on `0.0.0.0:8825`, and prints a QR code. Every
request needs the reach token; anything without it gets a 401 and never touches the daemon.
Websocket handshakes are checked too.

`frank reach pair` prints the same code without starting a server, for when it is already running
somewhere else. `frank reach rotate` mints a new token, which unpairs every device holding the old
one.

| Flag | What it does |
|---|---|
| `-p`, `--port` | The port. Default 8825, and fixed on purpose — a phone cannot be told a new one every morning. |
| `--host` | What to bind. Default `0.0.0.0`, because a phone cannot reach loopback. |
| `--advertise` | The address to hand the phone when something else fronts this: a reverse proxy, or a tunnel. Takes a host or a whole URL. |
| `--tls-certificate`, `--tls-key` | Serve TLS directly. |

### Where it can be reached from

The pairing code carries a **list** of addresses, best first, and the app races them on every
connect and keeps whichever answers. That is what makes the endpoint stable without anything
having a fixed IP: at home the phone uses the LAN address, away it uses the tailnet one, and the
connection does not notice.

The list is built from, in order:

1. Whatever `--advertise` said.
2. This machine's Tailscale name, if it is on a tailnet.
3. The LAN address the routing table says faces the network.

**Tailscale is the recommended shape**, and by some distance. The address is stable for the life
of the machine, WireGuard carries the token, and nothing is listening on a public port anywhere.
Install it on the Mac and on the phone, and `frank reach` finds the address by itself.

A reverse proxy terminating TLS on a hostname you own is the same bargain differently bought:
point it at `127.0.0.1:8825`, run `frank reach --host 127.0.0.1 --advertise https://frank.example.com`,
and the phone gets a real certificate.

What is **not** supported is forwarding port 8825 on your router. It is a bearer token over plain
HTTP; [`SECURITY.md`](../SECURITY.md) says to tunnel that rather than expose it, and this does not
change that advice.

### The token

Minted once, kept in `~/.local/share/frank/reach-token` at mode 0600, and unaffected by restarts —
unlike the daemon's own capability token, which is new on every boot and would unpair a device
every time the machine woke up.

It carries full control of the daemon. The QR code is meant for a phone, not for a room.

## Running the app

The app is a normal Expo project.

```sh
cd mobile && bun install
```

### On your phone, without Xcode

Install **Expo Go** from the App Store, then:

```sh
cd mobile && bun run start
```

Scan the QR code Expo prints. The app runs on your phone against the development server on your
Mac — which is also the machine `frank reach` is serving from, so both codes come from the same
laptop: Expo's to load the app, Frank's to pair it.

Everything the app uses — the camera for pairing, the keychain, the microphone — is in Expo Go, so
no native build is needed to use it properly.

### In a browser

```sh
cd mobile && bun run web
```

React Native Web, at a phone-sized window. Useful for looking at layout; the camera does not work
there, so pair by pasting the `frank://pair#…` link instead.

### As an installed app

`bunx eas build --profile preview --platform ios` builds a standalone app in Expo's cloud, which
is the path that does not need Xcode locally. Nothing in the app requires it.

## How it is put together

There is one interface, and it is `web/`. The phone runs it.

```
mobile/src
  app/
    index.tsx        a WebView onto the machine's own interface
    pair.tsx         the camera, and the token going into the keychain
  lib/connection.tsx which of the machine's addresses answers, and holding the pairing
  theme/             tokens, for the two screens above and nothing else
```

That is the whole application. The sessions list, the transcript, the tool rows and their
shimmer, the composer, the approval cards and the settings are all `web/src`, served by
`frank reach` from the same bundle the Tauri app puts in a window.

It began as a React Native port of those screens, and the reason it is not one any more is worth
recording. A port can be faithful on the day it is written and cannot *stay* faithful, because
nothing structurally prevents it drifting — and it drifted, in ways that were obvious to anyone
who used both: a "thinking" row the desktop deliberately does not render, a spinner where the
desktop shimmers, a workspace named `giovannigravili +1` where the desktop says
`giovannigravili, colima`. Each was a decision made a second time because reaching the first one
was inconvenient. None of them is reachable now.

### How the token gets in

The app opens `https://endpoint/?token=…` exactly once. `frank reach` answers that document with
an `HttpOnly` session cookie, and every script, font, event stream and websocket the page asks
for afterwards carries the token without the page ever holding it. A page cannot attach a
credential to its own subresources; a cookie is attached by the transport, and no script can read
it back out.

### What this makes into work on `web/`

Making the interface good on a phone is now work on the shared implementation, which is where it
belongs — a dialog that is unusable at 390pt is unusable in a narrow browser window too. That
hardening has started and is not finished:

- **Done.** Every dialog is full-bleed below `sm` — square corners, full height, a header and
  footer that stay put while the body scrolls between them, and safe-area padding top and bottom.
  Stated once in the dialog slot recipe in `web/src/components/ui/provider.tsx`, so every dialog
  inherits it. The settings dialog is full-bleed to match.
- **Still to do.** The model picker, the schedule form and the workspace dialogs have their own
  widths and want checking at 390pt. The composer's control row wraps but has not been looked at
  on a real device. The terminal panel assumes a pointer. Long-press and swipe affordances do not
  exist anywhere.

## Checks

```sh
cd mobile && bunx tsc --noEmit && bunx expo lint
```

The reach listener's guard is tested in `tests/test_reach.py`: that nothing without the token gets
through, HTTP or websocket, and that the token does not ride along to the daemon.
