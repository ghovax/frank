You just noticed that an artifact you opened failed to load for the user. The harness caught the failure and is handing it to you (the user has not been shown the raw error):

{{ payload }}

This is your own output to repair. Acknowledge it briefly in your own voice — as something you just caught — then diagnose the cause from the error above, fix the file, and re-open the *same* artifact in place (pass the same `artifact_id` back to `open_artifact`). Do not ask the user what went wrong, do not blame them, and do not describe this as an external report; simply fix it.
