Read a sibling turn in this session by its id. It returns the turn's current status and its artifact, which is the deliverable.

Use this to coordinate with a sibling A2A task id that reached you from outside. Check whether the sibling finished, read what it produced, and build on that.

**This is not how you get a background result, and it is not how you read a peer session.** A `search_web` handle (`search-…`) and a background `bash` handle (`bg-…`) are not readable tasks. Those results reach you on their own, so never call `read_turn` on one, and never poll with it. To look at a peer session, use `read_session`. A peer's answer arrives on its own, as a message.

Arguments:
  - turn_id: The id of a sibling turn that reached you from outside.
  - explanation: A short reason for the read, in the words the user reads. The interface shows it as this call's label.
