"""The LangGraph workflow (CLAUDE.md §7). Every user message enters through ``ingest``, where CODE
decides approval before the model runs. Every exit funnels through ``respond`` -> ``finalize``.

  START -> ingest -> approval_gate --approved/repeat--> execute -> verify -> respond
                                   --declined/expired--------------------> respond
                                   --other--> understand --ask/answer/limit/error--> respond
                                                         --propose--> hold -> respond
  respond --waiting approval--> await_approval (interrupt) -> ingest
          --waiting input----> ask_user        (interrupt) -> ingest
          --otherwise--> END

LangGraph re-runs an interrupted node from its start on resume, so ``execute`` does nothing but call
the idempotent executor, and the interrupt nodes do nothing but wait.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from errand.agent.finalize import Exit, finalize
from errand.agent.prompts import build_request
from errand.agent.state import ErrandState
from errand.agent.tools import ToolRouter
from errand.contract.errors import ErrorCode, ToolError
from errand.core import BudgetExhausted, Reader
from errand.model.base import Message

_CANT_HOLD = (ErrorCode.SLOT_UNAVAILABLE, ErrorCode.PROVIDER_NOT_FOUND, ErrorCode.INVALID_ARGUMENT)
_SUCCESS = ("confirmed", "already_done")


def _jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=list))


def summarize_proposal(row: Any, options: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    name = extra.pop("provider_name", None) or next(
        (o["name"] for o in options if o["provider_id"] == row["provider_id"]), None
    )
    return {
        "proposal_id": row["id"], "action": row["action"], "provider_id": row["provider_id"],
        "provider_name": name, "slot": row["slot"], "price": row["price"], "new_slot": row["new_slot"],
        "booking_id": row["booking_id"], "confirm_code": row["confirm_code"], **extra,
    }


def build_graph(rt: Any) -> StateGraph:
    g = StateGraph(ErrandState)

    def limit_exit(e: BudgetExhausted) -> dict[str, Any]:
        rt.emit("limit_reached", {"limit": e.reason})
        return {"exit": asdict(Exit("limit", reason=e.reason))}

    # ------------------------------------------------------------------ ingest: approval is decided HERE, by code
    def ingest(state: ErrandState) -> dict[str, Any]:
        text = state["user_message"]
        run_id = rt.new_run()
        rt.emit("run_start", {"run_id": run_id, "session_id": state["session_id"], "chars": len(text)})
        d = rt.guard.on_user_message(state["session_id"], text)
        rt.emit("decision", {"run_id": run_id, "kind": d.kind, "proposal_id": d.proposal_id, "approval_id": d.approval_id})
        pending = state.get("pending")
        row = rt.store.one("SELECT * FROM proposals WHERE session_id = ? AND status = 'pending'", (state["session_id"],))
        if row is None:
            pending = None
        elif not pending or pending.get("proposal_id") != row["id"]:
            pending = summarize_proposal(row, state.get("options", []))
        return {
            "run_id": run_id, "decision": asdict(d), "approval_id": d.approval_id, "pending": pending,
            "messages": [{"role": "user", "content": text}], "proposal_request": None, "outcome": None,
            "exit": None, "final": None, "waiting": None,
        }

    def approval_gate(state: ErrandState) -> dict[str, Any]:
        kind = state["decision"]["kind"]
        if kind == "declined":
            return {"exit": asdict(Exit("declined", proposal_id=state["decision"]["proposal_id"])), "pending": None}
        if kind == "expired":
            return {"exit": asdict(Exit("expired", proposal_id=state["decision"]["proposal_id"])), "pending": None}
        return {}

    def after_gate(state: ErrandState) -> str:
        d = state["decision"]
        if d["kind"] in ("approved", "repeat") and d.get("approval_id"):
            return "execute"
        return "respond" if state.get("exit") else "understand"

    # ------------------------------------------------------------------ understand: the bounded model loop
    def understand(state: ErrandState) -> dict[str, Any]:
        run_id = state["run_id"]
        budget = rt.budget(run_id)
        request = dict(state.get("request") or {})
        options = list(state.get("options") or [])
        read = Reader(rt.tools, rt.clock, rt.limits, budget, rt.emit)
        router = ToolRouter(read, rt.user_id, rt.emit)
        run_msgs: list[Message] = []

        def out(exit_: Exit, **more: Any) -> dict[str, Any]:
            return {"exit": asdict(exit_), "request": request, "options": options, **more}

        try:
            while True:
                budget.charge_model_turn()
                req = build_request(rt.clock.now().isoformat(timespec="minutes"), request, options,
                                    state.get("pending"), state.get("messages", []), run_msgs)
                t0 = rt.clock.monotonic()
                turn = rt.model.complete(req)
                rt.emit("model_call", {"run_id": run_id, "turn": budget.model_turns, "tools": list(req.tool_names),
                                       "model": getattr(rt.model, "name", type(rt.model).__name__),
                                       "latency_s": rt.clock.monotonic() - t0,
                                       "input_tokens": turn.usage.input_tokens, "output_tokens": turn.usage.output_tokens,
                                       "tool_calls": [c.name for c in turn.tool_calls]})
                if not turn.tool_calls:
                    return out(Exit("answer", text=turn.text))
                run_msgs.append(Message("assistant", turn.text or "", tuple(turn.tool_calls)))
                for i, c in enumerate(turn.tool_calls):
                    res = router.run(c)
                    if res.kind in ("propose", "ask"):
                        for skipped in turn.tool_calls[i + 1:]:
                            rt.emit("tool_call_ignored", {"tool": skipped.name})
                        if res.kind == "ask":
                            request.update({k: v for k, v in res.request_update.items() if v is not None})
                            for k, v in res.request_update.items():
                                if v is None:
                                    request.pop(k, None)
                            rt.emit("ask_user", {"run_id": run_id, "missing": res.payload["missing"],
                                                 "known": sorted(res.request_update)})
                            return out(Exit("ask", text=res.payload["question"], missing=res.payload["missing"]))
                        rt.emit("propose_requested", {"run_id": run_id, **res.payload})
                        return {"proposal_request": res.payload, "request": request, "options": options}
                    request.update(res.request_update)
                    if res.options is not None:
                        options = res.options
                    run_msgs.append(Message("tool", res.content, tool_call_id=c.id, name=c.name))
        except BudgetExhausted as e:
            return {**limit_exit(e), "request": request, "options": options}
        except Exception as e:  # model/backend failure or a bug: never hang, never stay silent
            rt.emit("understand_error", {"type": type(e).__name__})
            kind = "model_error"
            return out(Exit(kind, reason=type(e).__name__))

    def after_understand(state: ErrandState) -> str:
        return "hold" if state.get("proposal_request") else "respond"

    # ------------------------------------------------------------------ hold: deterministic code prepares the proposal
    def hold(state: ErrandState) -> dict[str, Any]:
        pr = state["proposal_request"] or {}
        run_id, session, options = state["run_id"], state["session_id"], state.get("options", [])
        read = Reader(rt.tools, rt.clock, rt.limits, rt.budget(run_id), rt.emit)
        try:
            if pr["action"] == "book":
                h = read("hold_slot", provider_id=pr["provider_id"], slot=pr["slot"])
                pid = rt.guard.propose_booking(session, rt.user_id, h)
                extra = {"search_price": h.search_price, "price_changed": h.price_changed}
            else:
                lb = read("list_bookings", user_id=rt.user_id)
                b = next((x for x in lb.bookings if x.booking_id == pr["booking_id"]), None)
                if b is None or b.status != "confirmed":
                    return {"exit": asdict(Exit("booking_not_found", action=pr["action"])), "proposal_request": None}
                pid = (rt.guard.propose_cancel(session, rt.user_id, b) if pr["action"] == "cancel"
                       else rt.guard.propose_reschedule(session, rt.user_id, b, pr["slot"]))
                extra = {"provider_name": b.provider_name}
        except BudgetExhausted as e:
            return {**limit_exit(e), "proposal_request": None}
        except ToolError as e:
            kind = "slot_unavailable" if e.code in _CANT_HOLD else "hold_failed"
            rt.emit("hold_failed", {"code": e.code})
            return {"exit": asdict(Exit(kind, reason=e.code, action=pr["action"])), "proposal_request": None}
        except Exception as e:
            rt.emit("understand_error", {"type": type(e).__name__, "node": "hold"})
            return {"exit": asdict(Exit("unexpected", reason=type(e).__name__)), "proposal_request": None}
        row = rt.store.proposal(pid)
        summary = summarize_proposal(row, options, **extra)
        rt.emit("proposal_created", {"run_id": run_id, "proposal_id": pid, "action": pr["action"]})
        return {"pending": summary, "proposal_request": None,
                "exit": asdict(Exit("proposal_ready", action=pr["action"], proposal=summary, proposal_id=pid))}

    # ------------------------------------------------------------------ execute: ONLY the idempotent executor
    def execute(state: ErrandState) -> dict[str, Any]:
        try:
            out = rt.executor.execute(state["approval_id"], rt.budget(state["run_id"]))
        except Exception as e:  # an executor bug must not claim anything; the attempt stays resolvable
            rt.emit("executor_exception", {"type": type(e).__name__})
            return {"outcome": {"kind": "unknown", "reason": f"executor_exception:{type(e).__name__}"}}
        return {"outcome": _jsonable(asdict(out))}

    # ------------------------------------------------------------------ verify: no success without a verified record
    def verify(state: ErrandState) -> dict[str, Any]:
        o = dict(state["outcome"] or {})
        kind, approval_id = o.get("kind", "unexpected"), state.get("approval_id")
        prop = rt.store.proposal(o["proposal_id"]) if o.get("proposal_id") else None
        action = prop["action"] if prop else None
        if kind in _SUCCESS:
            att = rt.store.latest_attempt(approval_id) if approval_id else None
            if not (att and att["status"] == "CONFIRMED" and att["verified_at"]):
                rt.emit("verify_downgrade", {"approval_id": approval_id, "claimed": kind})
                kind, o["reason"], o["booking"] = "unknown", "unverified_success", None
        proposal, pending = None, state.get("pending")
        if kind == "needs_reapproval" and o.get("new_proposal_id"):
            row = rt.store.proposal(o["new_proposal_id"])
            proposal = summarize_proposal(row, state.get("options", []))
            pending = proposal
        elif kind != "needs_reapproval":
            pending = None if kind in _SUCCESS or kind in ("slot_taken", "slot_conflict", "approval_invalid") else pending
        booking = o.get("booking") if kind in _SUCCESS else None
        rt.emit("verified", {"kind": kind, "reason": o.get("reason"),
                             "booking_id": booking.get("booking_id") if isinstance(booking, dict) else None})
        return {"pending": pending, "exit": asdict(Exit(
            kind, o.get("reason"), action, o.get("booking") if kind in _SUCCESS else None, o.get("diff"),
            proposal, o.get("proposal_id"),
        ))}

    # ------------------------------------------------------------------ respond: the single exit
    def respond(state: ErrandState) -> dict[str, Any]:
        exit_ = Exit(**(state.get("exit") or {"kind": "unexpected", "reason": "no_exit"}))
        final = finalize(exit_)
        rt.emit("final", {"run_id": state["run_id"], "status": final.status, "kind": final.kind,
                          "reason": final.reason, "waiting": final.waiting})
        if final.kind in _SUCCESS and exit_.proposal_id:  # arms "the very next reply" repeat window
            rt.guard.mark_result_reported(state["session_id"], exit_.proposal_id)
        return {"final": asdict(final), "waiting": final.waiting,
                "messages": [{"role": "assistant", "content": final.message}]}

    def after_respond(state: ErrandState) -> str:
        return {"approval": "await_approval", "input": "ask_user"}.get(state.get("waiting") or "", END)

    # ------------------------------------------------------------------ the two waiting points (interrupts)
    def wait_node(kind: str):
        def node(state: ErrandState) -> dict[str, Any]:
            f = state["final"] or {}
            text = interrupt({"kind": kind, "message": f.get("message"), "proposal_id": f.get("proposal_id")})
            return {"user_message": text}
        node.__name__ = f"wait_{kind}"
        return node

    def traced(name: str, fn: Any) -> Any:
        """State transitions for the trace. `ingest` has no enter event (its state still holds the previous
        run's id; `run_start` is its enter). The two wait nodes are not traced: `final.waiting` says it.
        A crash leaves an enter without an exit, so a re-run of `execute` shows as two enters."""
        def node(state: ErrandState) -> dict[str, Any]:
            t0 = rt.clock.monotonic()
            if name != "ingest":
                rt.emit("node_enter", {"node": name, "run_id": state.get("run_id")})
            out = fn(state)
            rt.emit("node_exit", {"node": name, "run_id": out.get("run_id") or state.get("run_id"),
                                  "latency_s": rt.clock.monotonic() - t0,
                                  "exit_kind": (out.get("exit") or {}).get("kind"), "waiting": out.get("waiting")})
            return out
        node.__name__ = name
        return node

    for name, fn in (("ingest", ingest), ("approval_gate", approval_gate), ("understand", understand), ("hold", hold),
                     ("execute", execute), ("verify", verify), ("respond", respond)):
        g.add_node(name, traced(name, fn))
    for name, fn in (("await_approval", wait_node("approval")), ("ask_user", wait_node("input"))):
        g.add_node(name, fn)
    g.add_edge(START, "ingest")
    g.add_edge("ingest", "approval_gate")
    g.add_conditional_edges("approval_gate", after_gate, {"execute": "execute", "respond": "respond", "understand": "understand"})
    g.add_conditional_edges("understand", after_understand, {"hold": "hold", "respond": "respond"})
    g.add_edge("hold", "respond")
    g.add_edge("execute", "verify")
    g.add_edge("verify", "respond")
    g.add_conditional_edges("respond", after_respond, {"await_approval": "await_approval", "ask_user": "ask_user", END: END})
    g.add_edge("await_approval", "ingest")
    g.add_edge("ask_user", "ingest")
    return g
