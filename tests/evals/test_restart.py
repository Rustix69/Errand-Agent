"""Crash injection through the FULL graph. The process 'dies' at a precise point, then both databases (and the
LangGraph checkpoint file) are reopened by a new process: startup recovery runs, then the run resumes."""

import pytest

from errand.core import Hooks
from errand.mocks import SimulatedCrash
from tests.core.helpers import ProcessKilled
from tests.evals.scripts import pick, search

FIND = "find a plumber in Indiranagar this week"
BOOK = [search(), pick()]


def force(tool, kind, **kw):
    return lambda a: a.env.world.sim.force(tool, kind, **kw)


def next_node(a):
    return a.agent.graph.get_state(a.agent.cfg).next


def test_r1_kill_between_the_attempt_row_and_book(scenario):
    def kill(att):
        raise ProcessKilled()

    run = scenario(
        "R1", "KILL after the attempt row is written, before book is called; reopen both DBs; resume",
        "recovery marks the attempt NOT_EXECUTED (verified); execute re-runs -> needs_reapproval, 0 book calls; "
        "a fresh proposal + approval books exactly once",
        BOOK, [FIND], env_kw={"hooks": Hooks(before_tool_call=kill)},
    )
    with pytest.raises(ProcessKilled):
        run.say("yes")
    dead = run.a
    assert next_node(dead) == ("execute",) and dead.env.count("book") == 0
    assert [x["status"] for x in dead.env.store.attempts()] == ["PENDING"]

    b = run.restart([pick(from_state=True)])  # new process, no hooks, same files
    assert [d["result"] for d in b.of("recovery_resolved")] == ["not_executed"]
    assert next_node(b) == ("execute",)  # the checkpoint survived: the interrupted node is next
    r = b.agent.resume()  # LangGraph re-runs `execute` from its start
    run.replies.append(r)
    assert (r.status, r.kind, r.reason) == ("NEEDS_INPUT", "needs_reapproval", "not_executed")
    assert b.env.count("book") == 0 and run.facts()["agent_bookings"] == 0
    assert [d["node"] for d in b.of("node_enter") if d["node"] == "execute"] == ["execute", "execute"]  # two processes

    run.say("yes, please try again")  # no pending proposal: an ordinary message, so the model proposes again
    assert run.replies[-1].waiting == "approval"
    run.say("yes")
    assert run.replies[-1].kind == "confirmed" and b.env.count("book") == 1 and len(run.mine()) == 1
    assert [x["status"] for x in b.env.store.attempts()] == ["NOT_EXECUTED", "CONFIRMED"] and run.facts()["approvals"] == 2


def test_r2_crash_after_the_booking_committed(scenario):
    run = scenario(
        "R2", "CRASH right after book committed; reopen both DBs; resume",
        "recovery verifies the booking CONFIRMED with no new book call; resume reports it verified; a repeat 'yes' is "
        "already_done; exactly 1 booking",
        BOOK, [FIND],
    )
    with pytest.raises(SimulatedCrash):
        run.say("yes", before=force("book", "crash_after_commit"))
    dead = run.a
    assert next_node(dead) == ("execute",) and dead.env.count("book") == 1 and len(run.mine()) == 1  # it DID happen
    assert [x["status"] for x in dead.env.store.attempts()] == ["PENDING"]  # ... but the agent never learned it

    b = run.restart()
    assert [d["result"] for d in b.of("recovery_resolved")] == ["confirmed"]
    assert [(x["status"], bool(x["verified_at"])) for x in b.env.store.attempts()] == [("CONFIRMED", True)]
    r = b.agent.resume()
    run.replies.append(r)
    assert (r.status, r.kind) == ("DONE", "already_done")  # recovery had already verified it
    assert b.env.count("book") == 1 and len(run.mine()) == 1
    n = len(b.env.calls())
    assert run.say("yes").kind == "already_done" and len(b.env.calls()) == n  # a repeat does nothing
    assert len(run.mine()) == 1 and run.facts()["approvals"] == 1


def test_r3_restart_while_waiting_for_approval(scenario):
    run = scenario(
        "R3", "Restart while a proposal waits for approval",
        "the pending proposal and the interrupt survive in checkpoints.db / agent.db; 'yes' after the restart books once",
        BOOK, [FIND],
    )
    before = run.a.agent.state()["pending"]["proposal_id"]
    b = run.restart([])
    assert b.agent.state()["pending"]["proposal_id"] == before and next_node(b)  # paused at the interrupt
    run.say("yes")
    assert run.replies[-1].kind == "confirmed" and b.model.calls == 0  # the new process never needed the model
    assert len(run.mine()) == 1 and b.env.count("book") == 1 and run.facts()["approvals"] == 1


def test_r4_repeated_yes_after_a_restart(scenario):
    run = scenario(
        "R4", "'yes' again right after a restart that followed a completed booking",
        "the repeat window is persisted: already_done, 0 new world calls, 0 model calls, still 1 booking",
        BOOK, [FIND, "yes"],
    )
    assert run.replies[-1].kind == "confirmed"
    b = run.restart([])
    n = len(b.env.calls())
    r = run.say("yes")
    assert r.kind == "already_done" and len(b.env.calls()) == n and b.model.calls == 0
    assert len(run.mine()) == 1 and b.env.count("book") == 1
