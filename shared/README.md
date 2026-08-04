# `shared/` — what both clients are made of

The desktop and the phone are two renderers over one daemon. Everything that decides *what* to
show lives here and is written once; only the drawing is written twice.

That split is not a compromise, it is the boundary the platforms actually have. The desktop is
React DOM and Chakra — `<Flex>`, `<Badge>`, CSS. The phone is React Native — `<View>`, `<Text>`,
no DOM and no stylesheet. A Chakra component cannot render on a phone and a React Native view
cannot render in a browser, so a literally shared component tree would mean rewriting the desktop
in React Native primitives and losing xterm, pdf.js, Mermaid and every other thing that assumes a
document. The duplication worth removing is not the markup. It is the *decisions* — and those
were what had drifted: one client said "Medium risk" and the other said "medium", one named a
workspace after all its locations and the other invented a `+1`.

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

Icons are the interesting edge: the desktop draws with `react-icons/lu` and the phone with
`lucide-react-native`, which are different packages exporting different objects. So `tools.ts`
names the glyph — `"terminal"`, `"file-text"` — and each client maps that name to its own
package's component. One decision about which glyph means `bash`; two tiny tables turning a name
into something drawable.

## How each side reaches it

The web client resolves `@shared/*` through `tsconfig.json`. The phone resolves it the same way,
plus a Metro `watchFolders` entry so the bundler follows the files out of `mobile/`.
