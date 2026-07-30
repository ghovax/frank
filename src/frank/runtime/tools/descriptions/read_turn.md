Read a sibling turn in this session by its id, returning its current status and artifact (deliverable).

Use this to coordinate with externally supplied sibling A2A task ids: check whether a sibling has finished and read what it produced, then build on it.

This is NOT how you retrieve background results, and it is not how you read a peer session. A search_web ("search-…") or background-bash ("bg-…") handle is not a readable task — those results are delivered to you automatically when ready, so never call read_turn on one and never use it to poll. To look at a peer session, use read_session; a peer's answer arrives on its own as a message.

Arguments:
  - turn_id: The id of an externally supplied sibling turn to read.
  - explanation: A concise, user-facing description of why you are reading this task — shown as the label for this call.
