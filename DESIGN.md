# Design note

**State model.** A LangGraph `StateGraph` carries a JSON-only `ErrandState`, checkpointed in `checkpoints.db`:
`ingest → approval_gate → {execute → verify | memory_op → capture → understand → (hold)} → respond`, plus two interrupt
nodes for waiting on the user. What must never be wrong lives outside the graph, in SQLite (`agent.db`): `proposals`,
`approvals`, `booking_attempts` (PENDING / CONFIRMED / FAILED / NOT_EXECUTED), memory and proactive tables. Each
message is bounded (8 model turns, 12 tool calls, 30 s) and every exit goes through one `finalize`.

**Approval.** The model has 7 tools: reads, `propose_*` and `ask_user`; never `book`, `cancel` or `reschedule`.
Only `ApprovalGuard.on_user_message` can insert an approval, from the raw user text, and only if it exactly matches an
allowlisted reply or the displayed `CONFIRM <code>` ("yes but make it 11" is not one). The approval request is built by
code from the stored terms. An approval is single-use, lasts 180 s and binds a fingerprint of action, provider, slot,
price and hold/booking id. The executor re-reads live state and compares: a mismatch stops it (`approval_invalid`, or a
fresh proposal; an expired hold is re-held once, only if provider, slot and price are unchanged). Review text is
wrapped as `<untrusted_data>` and scanned; the structure above is the real defence.

**Idempotency and recovery.** In one transaction the executor consumes the approval and inserts a PENDING attempt with
a deterministic key (`<action>-<approval>-<n>`); only then does it call the tool. A timeout is resolved with
`get_booking_status`; a retry reuses the key, only if nothing was booked. Success is claimed only for a CONFIRMED,
verified attempt. At startup `resolve_pending` settles every PENDING attempt through the source of truth, using a
reader that cannot call `book`. `execute` is its own node because LangGraph re-runs an interrupted node.

**Memory and context.** Memories are explicit or inferred. A SQL view returns only rows that are not forgotten, not
replaced and, if inferred, confirmed; Python ranks (tags, keyword overlap) only what the view returned. Inspect,
correct, forget, confirm and plain statements ("I'm in Jadavpur") are handled by code. The context builder fills a
3,000-token budget in priority order (rules, state, memories, recent turns, a counts-only summary) and logs each
request's memory ids and token count in `context_log`.

**Observability.** One JSONL trace per run and per proactive poll that found something: node transitions, model calls with token usage,
tool names and arguments, a short summary of each result, latency and an *estimated* cost; time comes from an injected
clock, so offline traces are reproducible. Excerpt of `artifacts/traces/timeout-after-success/run_000002.jsonl`
(scripted run; `book` commits, then times out):

```
0.000  decision          approval_id="apr_000001" kind="approved" proposal_id="prop_000001"
0.000  attempt_pending   action="book" key="book-apr_000001-0" ref="hold_000001" seq=0
0.000  tool_call         book {"hold_id": "hold_000001", "idempotency_key": "book-apr_000001-0"}
3.000  tool_error        book timeout retryable=True                                       3.000s
3.000  tool_call         get_booking_status {"idempotency_key": "book-apr_000001-0"}
3.000  tool_result       get_booking_status status=confirmed booking_id=bk_000007 hold_status=booked
3.000  attempt_confirmed key="book-apr_000001-0"
3.000  verified          booking_id="bk_000007" kind="confirmed" reason=null
3.000  final             DONE / confirmed
```

The "yes" was approved by code (no model call); the status check, not the timeout, decides the outcome; there is one
`book` call. In `run_000001.jsonl` a model call reads `turn 1 -> search_providers in=808 out=31 ~$0.002889 est
(scripted)` (synthetic tokens: the model is scripted).

**Trade-offs, on purpose.**
- *Hand-written state machine; the graph is for flow only.* Approval and attempt state are SQL-backed code with unique
  keys, since a graph node can re-run on resume. Cost: state lives in two places, reconciled by `resume()` and recovery.
- *Approval tied to what the user saw.* The terms shown, the fingerprint and the executor's check use the same stored
  terms. Cost: even a ₹1 re-quote needs a new "yes".
- *SQLite behind `MemoryStore`; production would use pgvector.* A test runs the graph, context builder and poller on a
  store implementing only that interface. Cost: ranking is crude (tags and shared words).
- *The poller is plain code, not graph nodes.* No user turn, no model call: deterministic and testable. Cost: it runs
  between CLI inputs, or on a web-server timer that skips a tick while a message runs; there is no separate worker.
- *20% price cap.* A replacement costs at most 120% of the cancelled booking (no structured budget is stored); the
  quoted price is still shown for approval.
