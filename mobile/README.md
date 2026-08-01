# Frank, on a phone

An Expo client for the Frank daemon. It is a **client** — it contains no harness and starts no
daemon. The Mac has to be awake, `frankd` running, and `frank reach` serving.

Full guide: [documentation/mobile.md](../documentation/mobile.md).

## Running it

```sh
bun install
bun run start
```

Then install Expo Go on your phone and scan the code. Everything the app uses — the camera for
pairing, the keychain, the microphone — is in Expo Go, so no native build is needed.

On the Mac, in another terminal:

```sh
frank reach
```

Scan *that* code from inside the app to pair. It carries a token with full control of the daemon,
so show it to a phone rather than to a room.

`bun run web` renders the same app in a browser via React Native Web, which is useful for looking
at layout. The camera does not work there — pair by pasting the `frank://pair#…` link.

## Checks

```sh
bunx tsc --noEmit
bunx expo lint
bun run check:events   # the event types still match the harness's schema
```

## Notes for whoever edits this next

**Expo has changed.** Read the versioned docs at <https://docs.expo.dev/versions/v57.0.0/> rather
than working from memory. This is SDK 57, React Native 0.86, the New Architecture, React Compiler
on.

**The event union is generated, not written.** `src/lib/generated/events.ts` comes from the
harness's own Pydantic models by way of `web/src/lib/generated/events.schema.json`. Editing it by
hand is a change the next `bun run build:events` throws away.

**Messages are replaced, never mutated.** `src/lib/transcript.ts` keeps a mutable *array* on
purpose — a turn emits one part per token — but every message in it is replaced rather than
written through. A row is memoised on the identity of its message, so a field assigned in place is
a change no component can see: correct state, stale render, and nothing anywhere reporting a
problem.

**Nothing outside `src/theme` names a colour.** The tokens are a transcription of the desktop's,
with the semantic names kept, so the two clients can be compared.
