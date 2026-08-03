The search runs in the background. Its results reach you on their own, as a `web_search_completed` message that carries this same `job_id`.

Do not call `read_turn` on it, and do not poll for it. Carry on. The results arrive by themselves.
