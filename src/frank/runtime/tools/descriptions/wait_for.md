Pause for a fixed number of seconds, then continue. The wait is cheap and deliberate, and it needs no model round trip while it runs.

Use this to poll, instead of hammering. Where you wait for something to become ready — a server to start, a file to appear, a background job you started — do the check. If the thing is not ready, `wait_for` a few seconds and check again. Do not issue the same call twice in a row and expect a different answer. To learn whether a repeated action changed anything, read the earlier call's `output_file` again.

Prefer a short wait and another check to one long sleep. A Stop interrupts the wait at once.

Do not use `wait_for` to pass time when you have nothing to check. End your turn instead. The harness re-engages you when the background work finishes.

Arguments:
  - seconds: How long to wait before you continue. Prefer a few seconds, then check again.
  - explanation: A short reason for the wait, in the words the user reads.
