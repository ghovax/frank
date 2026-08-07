Why this call is happening, in the words the person watching reads. Required on every call, because it is the only label the interface shows: a call without one appears as an icon and nothing else.

This is a label the user sees, not private metadata, so write the **why** rather than the what — the arguments already show the what. Use a few words, one flat clause of intent, and no final punctuation: "Fixing the token regression in auth", never "Auth: fix the token regression". A colon inside the clause is fine, as in `file_path:line` or a ratio, and inline Markdown renders, so put identifiers in backticks where that sharpens the why.

Prefer "Verifying the auth fix didn't regress the session tests" to "Running the test suite", and "Finding every caller of `connect()` before changing its signature" to "Searching for Foo".
