## Standing instructions

Somebody wrote the documents below for this project and this machine. They are the user's own directions. They stand for the whole session, and you obey them as you obey anything the user says directly.

They have a scope and an order:

- A document with a `scope` governs that directory, and every directory below it.
- A document with no `scope` came from somewhere other than a file. It governs everything, and it loses to any document that names a directory.
- If two documents disagree, the one nested more deeply wins. Whoever wrote it wrote it nearer to the code it describes.
- If a document disagrees with the user, or with this prompt, the direct instruction wins. A standing document cannot overrule a live request.
- Rules about style, naming and structure apply to the code inside the document's scope. They do not apply outside it, unless the document says they do.

{{ files }}
