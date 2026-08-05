# `shared/` — what the desktop and the phone both read

The phone is a WebView onto the same interface the desktop shows, so there is one implementation
of the screens and nothing to keep in step. What is still genuinely two programs is the shell
around that page: the phone's pairing screen and machine list are its own, and they show the same
words in the same languages as everything else.

So this directory holds what has no renderer in it — text, names, colours, and the wire contract.
Anything here can be read by a React DOM page, by a React Native screen, or by neither.

## What belongs here

Anything with no import from `react-dom`, `@chakra-ui/*`, `react-native`, or `next/*`:

| | |
|---|---|
| `messages/` | Every string either client shows. `en` is the shape; `ja` mirrors its keys. |
| `generated/` | The wire event union, generated from the harness's Pydantic models by `scripts/generate_event_schema.py`. |
| `labels.ts` | A reader for the catalogue, so a client with no i18n framework still gets the same words. |
| `workspace.ts` | What a workspace and a location are called. |
| `status.ts` | What a turn's state is called, and in which colour. |
| `tools.ts` | What a tool call is called, and which glyph stands for it — by name, not by component. |

## What does not

Components. Styling. Anything that imports a renderer.

`tools.ts` names a glyph — `"terminal"`, `"file-text"` — rather than exporting a component, so
what a tool call *is called* and what it is *drawn with* are one decision, made where the other
decisions about that tool are made.

## How each side reaches it

The web client resolves `@shared/*` through `tsconfig.json`. The phone resolves it the same way,
plus a Metro `watchFolders` entry so the bundler follows the files out of `mobile/`.
