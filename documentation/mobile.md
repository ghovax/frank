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

```
mobile/src
  app/                 the routes: the session list, one conversation, pairing, settings, schedules
  components/          the transcript, the composer, the gates, and the primitives under them
  lib/
    api.ts             the daemon, transcribed from web/src/lib/api.ts
    connection.tsx     the pairing, the keychain, and the endpoint race
    transcript.ts      parts folded into a transcript — the same reducer for live and replay
    use-chat.ts        one turn, driven and watched
    dictation.ts       recording, and the samples the transcriber wants
    glyphs.ts          a shared glyph name as a lucide-react-native component
  theme/               the desktop's design tokens, as values React Native can use
```

Almost nothing in `mobile/src` decides anything. What to show lives in
[`shared/`](../shared/README.md) and is read by both clients:

| | |
|---|---|
| `shared/messages/` | Every string either client shows. The desktop reads it through `next-intl`; the phone reads it through `shared/labels.ts`. |
| `shared/generated/` | The wire event union, from the harness's Pydantic models. Generated once, into `shared/`. |
| `shared/workspace.ts` | What a workspace and a location are called. |
| `shared/status.ts` | What a turn's state is called, and in which colour. |
| `shared/tools.ts` | What a tool call is called, and which glyph stands for it. |

The typefaces are shared the same way: Metro watches `web/public/fonts`, so one set of files in
the repository is bundled by both.

What cannot be shared is components. The desktop is React DOM and Chakra; the phone is React
Native, which has no DOM and no stylesheet. The seam between them is deliberately narrow — each
client has one small table (`glyphs.ts`) turning a shared glyph *name* into its own icon
package's component, and that is the whole of it.

### What the port changed, and why

**Streaming.** `fetch` in React Native resolves its whole body before it answers, so a server-sent
event tail would arrive when the turn was already over. Every request goes through `expo/fetch`,
the WinterCG implementation that streams.

**Coalescing.** The desktop copies its mutable transcript into React once per animation frame. A
phone in a pocket is not painting, so a frame scheduled then never arrives — and because a pending
frame also means "do not schedule another", the transcript would freeze at whatever was on screen
when the app went away. The mobile version schedules a frame *and* a timer, and whichever fires
first cancels the other.

**Dictation.** The desktop taps the browser's audio graph, which hands out float32 and resamples
the microphone for you. A phone has no audio graph, so the same contract is met from the other
side: record uncompressed at exactly 16 kHz and strip the WAV header. The wire format is unchanged
— the endpoint takes raw mono float32 either way.

Android is the gap, and it is stated rather than hidden: its recorder encodes, with no
uncompressed option to ask for, so honouring that contract there would mean shipping a decoder.
The daemon is macOS-only, so the phone at the other end of it is overwhelmingly an iPhone, and the
app says so rather than failing after somebody has spoken a paragraph into it.

**Density.** The desktop composer drops labels as it narrows — provider name, then the sandbox
label, then the permission label, then the counts — on the rule that it is better to drop a label
than to truncate it. A phone is below the last of those breakpoints before it starts, so the
mobile composer is that ladder's final rung: icons and values, with the words in the sheet that
opens when you tap one.

## Checks

```sh
cd mobile && bunx tsc --noEmit && bunx expo lint
```

The reach listener's guard is tested in `tests/test_reach.py`: that nothing without the token gets
through, HTTP or websocket, and that the token does not ride along to the daemon.
