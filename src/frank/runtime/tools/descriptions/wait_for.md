Pause for a fixed number of seconds, then continue — a cheap, intentional wait with no model round-trip while it runs.

Use this to POLL instead of hammering: when you are waiting on something to become ready (a server to come up, a file to appear, a background job you started), do the check, and if it is not ready, wait_for a few seconds and check again — rather than re-issuing the same call back-to-back and expecting a different result. To tell whether a repeated action changed anything, re-read the prior call's ``output_file``.

Prefer short waits and re-check over one long sleep; a Stop interrupts the wait immediately. Do NOT use wait_for to pass time when you have nothing to check — end your turn instead, and the harness re-engages you when background work completes.

Arguments:
  - seconds: How long to wait before continuing. Prefer small values (a few seconds) and re-check.
  - explanation: A concise, user-facing reason for the wait.
