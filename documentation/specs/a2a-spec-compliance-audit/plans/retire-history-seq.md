# Retire the Hand-Computed History `seq`

This is a small correctness-and-simplicity plan. `AppendOnlyTaskStore.task_history` carries **two** ordering columns: a DB-native `row_id` (an autoincrement primary key the database assigns on every insert) and a hand-computed per-task `seq` (0-based, contiguous, assigned by the store as `MAX(seq) + 1`). The `seq` computation is a read-then-insert — read the current max, then insert at max+1 — which is exactly the shape that let two concurrent saves collide on the same position (the race the durable-goal work exposed and the [self-managing-turn plan](./self-managing-turn.md) fixed by reading the max authoritatively). But the deeper point is that `seq` is redundant: the database already assigns a monotonic position on every insert, atomically and without a read, in `row_id`. This plan retires `seq` entirely and makes `row_id` the single ordering key, so the "next position" is never computed in application code again and the collision is impossible by construction rather than merely avoided.

## Where we are today

`task_history` is `(row_id INTEGER PK AUTOINCREMENT, task_id, seq INTEGER, message)` with a unique constraint on `(task_id, seq)` and an index on `(task_id, row_id)`. The two columns do overlapping jobs:

- **`row_id`** is the database's own monotonic insert order. It already drives the wire-facing surface: `tasks_for_context` paginates by it (`before_row_id` / `next_before_row_id`), reconstructs per-page order with it, and the artifact table orders by it exclusively. It needs no application logic — the database assigns it.
- **`seq`** is a per-task logical position the store maintains by hand. It is used for four things, each of which `row_id` can do: (1) ordering a task's history on read (`load_checkpoint`, `get`, `tasks_for_context` all `ORDER BY seq`); (2) finding a task's latest message (`MAX(seq)` per task in `tasks_for_context`, matched against each row); (3) the unique `(task_id, seq)` constraint that made a duplicate position an error; and (4) the next append position, `MAX(seq) + 1`, computed by `_history_count` and written as `seq = persisted + offset`.

The append path (4) is the liability. It reads the current max and inserts at max+1, so two saves of the same task that read the same max compute the same `seq`. Today that is contained by reading `MAX(seq)` inside the write transaction, but it is still application code reproducing what the database's autoincrement already does correctly and for free.

## The design

Make `row_id` the single ordering key and delete `seq`.

- **Schema.** Drop the `seq` column and the `uq_task_history_seq` unique constraint. Keep `row_id` as the autoincrement primary key, and set the table's `sqlite_autoincrement=True` so the database guarantees strictly increasing, **never-reused** ids — the property that makes `row_id` safe as a permanent ordering key even after the tail of a task is deleted by compaction (a bare `INTEGER PRIMARY KEY` may reuse the largest id after its row is deleted; the `AUTOINCREMENT` keyword forbids that).
- **Append.** Insert `(task_id, message)` and let the database assign `row_id`. No `MAX`, no count, no cached position — so `_history_count` is deleted outright, and two concurrent appends can never collide because the database serializes id assignment atomically. This is the whole point: the read-then-insert race is gone by construction, not by careful locking.
- **Order on read.** Every `ORDER BY seq` becomes `ORDER BY row_id`. Within a task, insert order and `row_id` order are identical, and compaction (below) preserves that, so the reconstructed history is byte-for-byte the same sequence as today.
- **Latest per task.** `MAX(seq)` per task becomes `MAX(row_id)` — the last-inserted row is the latest message. `tasks_for_context` already computes a per-task min `row_id` for its cursor; it gains a per-task max `row_id` and drops the `seq` comparison.
- **Compaction.** `_compact_persisted_history` rewrites a task's history in place: it reads the existing rows in `row_id` order, overwrites the first *M* rows' messages with the *M* compacted messages (their `row_id`s, and thus their order, unchanged), appends any surplus as new rows (new higher `row_id`s, still in order), and deletes the remaining tail **by `row_id`** (the rows beyond the first *M*) instead of by `seq >= M`. The result is the same ordered, in-place-rewritten history, keyed on `row_id`.

## No backward compatibility

Per the standing mandate, there is no migration path and none is needed: the schema changes and existing databases are disposable dev state. The table is created fresh with the new shape; no data is carried across. Nothing outside the store reads `seq` — it never crosses the store's boundary (the wire and the API already speak `row_id` through the pagination cursor), so retiring it is contained entirely within `task_store.py`.

## Build order

One focused, self-contained change to `task_store.py`: drop the `seq` column + unique constraint and set `sqlite_autoincrement`; delete `_history_count`; make `save`'s non-terminal append a bare insert; repoint every `ORDER BY seq` to `row_id`; swap the latest-per-task `MAX(seq)` for `MAX(row_id)` in `tasks_for_context`; and rewrite `_compact_persisted_history`'s tail delete to key on `row_id`. It lands in one step because the column cannot be half-removed.

## Testing

The bar is behavior-identical reads and a now-impossible collision. Verified against the fake-LLM turn harness plus targeted store tests: a task's reconstructed history is identical to today across plain appends and across a terminal compaction that reduces the message count; `tasks_for_context` returns the same latest message and the same pagination as before; and — the motivating case — a burst of concurrent saves of the same task never collides and never needs a retry, because no position is computed in application code at all. The self-managing-turn harness (which drove the original flake) stays green.

## Notes

`row_id`s are globally monotonic rather than per-task 0-based, so a task's rows are ordered but not numbered `0..n`; nothing depends on the numbering, only the order. A 64-bit autoincrement is not a practical exhaustion concern. And with `seq` and its unique constraint gone, the store's only invariant on history is "insert order is truth," which the database owns — one fewer hand-maintained invariant to get wrong.
