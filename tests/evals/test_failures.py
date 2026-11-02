"""Tool failures, injected through the mock's forced outcomes (and one executor hook). The agent must never
claim success it did not verify, never re-book blindly, and never hang."""

from errand.core import Hooks
from errand.model import ModelError, call, say
from tests.evals.scripts import pick, search, tool_json

FIND = "find a plumber in Indiranagar this week"
BOOK = [search(), pick()]


def force(tool, kind, **kw):
    return lambda a: a.env.world.sim.force(tool, kind, **kw)


def book_keys(a):
    return [c["args"]["idempotency_key"] for c in a.env.calls("book")]


def test_f1_timeout_after_success_is_verified_not_retried(scenario):
    run = scenario(
        "F1", "book times out AFTER the booking committed",
        "the source of truth is asked; reported confirmed and verified; exactly 1 book call and 1 booking",
        BOOK, [FIND],
    )
    r = run.say("yes", before=force("book", "timeout_after_commit"))
    a = run.a
    assert (r.status, r.kind) == ("DONE", "confirmed") and a.env.count("book") == 1 and len(run.mine()) == 1
    assert [d["code"] for d in a.of("tool_error") if d["tool"] == "book"] == ["timeout"]
    assert a.env.count("get_booking_status") >= 2  # the pre-check and the answer after the timeout
    (att,) = a.env.store.attempts()
    assert att["status"] == "CONFIRMED" and att["verified_at"] and att["error_code"] is None


def test_f2_timeout_before_commit_retries_with_the_same_key(scenario):
    run = scenario(
        "F2", "book times out BEFORE committing anything",
        "truth says not booked; ONE retry with the SAME idempotency key; 1 booking; confirmed",
        BOOK, [FIND],
    )
    r = run.say("yes", before=force("book", "timeout_before_commit"))
    a = run.a
    assert r.kind == "confirmed" and len(run.mine()) == 1
    assert a.env.count("book") == 2 and len(set(book_keys(a))) == 1


def test_f3_unverifiable_timeout_is_not_claimed_and_recovery_never_rebooks(scenario):
    box = []
    hooks = Hooks(before_tool_call=lambda att: box[0].env.world.sim.force("get_booking_status", "unavailable", times=3))
    run = scenario(
        "F3", "book times out after committing AND the source of truth is unavailable",
        "TOOL_FAILURE 'unknown', no success claimed, attempt stays PENDING; after a restart recovery verifies it "
        "CONFIRMED without any new book call",
        BOOK, [], env_kw={"hooks": hooks},
    )
    box.append(run.a)
    run.say(FIND)
    r = run.say("yes", before=force("book", "timeout_after_commit"))
    a = run.a
    assert (r.status, r.kind) == ("TOOL_FAILURE", "unknown")
    (att,) = a.env.store.attempts()
    assert att["status"] == "PENDING" and not att["verified_at"]  # never claimed
    assert a.env.count("book") == 1
    run.restart()  # startup recovery asks the source of truth, which is reachable again
    assert [d["result"] for d in run.a.of("recovery_resolved")] == ["confirmed"]
    (att,) = run.a.env.store.attempts()
    assert att["status"] == "CONFIRMED" and att["verified_at"] and run.a.env.count("book") == 1 and len(run.mine()) == 1


def test_f4_book_unavailable_on_every_retry_fails_cleanly(scenario):
    run = scenario(
        "F4", "book is unavailable (503) on every retry",
        "bounded retries (3) with ONE key, then TOOL_FAILURE; attempt FAILED (a clean 503: nothing was committed); 0 bookings",
        BOOK, [FIND],
    )
    r = run.say("yes", before=force("book", "unavailable", times=3))
    a = run.a
    assert r.status == "TOOL_FAILURE" and run.facts()["agent_bookings"] == 0
    assert a.env.count("book") == 3 and len(set(book_keys(a))) == 1
    assert [x["status"] for x in a.env.store.attempts()] == ["FAILED"]


def test_f5_cancel_survives_the_first_call_503(scenario):
    def propose_cancel(request):
        b = next(b for b in tool_json(request, "list_bookings")["bookings"]
                 if b["service"] == "home_cleaning" and b["is_upcoming"])
        return call("propose_cancel", booking_id=b["booking_id"])

    run = scenario(
        "F5", "cancel: the first call fails with a transient 503, the retry succeeds",
        "2 cancel calls with the SAME key; booking cancelled; attempt CONFIRMED and verified",
        [call("list_bookings"), propose_cancel], ["cancel my cleaning", "yes"],
    )
    a = run.a
    assert run.replies[-1].kind == "confirmed" and a.env.count("cancel") == 2
    assert len({c["args"]["idempotency_key"] for c in a.env.calls("cancel")}) == 1
    assert [b["status"] for b in a.env.user_bookings() if b["provider_id"] == "cl_04"] == ["cancelled"]
    (att,) = a.env.store.attempts()
    assert att["action"] == "cancel" and att["status"] == "CONFIRMED" and att["verified_at"]


def test_f6_reschedule_slot_conflict_then_another_slot(scenario):
    def move_to(hhmm):
        def step(request):
            ac = next(b for b in tool_json(request, "list_bookings")["bookings"]
                      if b["service"] == "ac_servicing" and b["is_upcoming"])
            return call("propose_reschedule", booking_id=ac["booking_id"], slot=f"2026-11-07T{hhmm}")
        return step

    run = scenario(
        "F6", "Reschedule the AC servicing to Saturday 10:00, a configured conflict slot",
        "slot_conflict -> NEEDS_INPUT, booking unchanged; a fresh proposal for 11:00 + fresh approval reschedules once",
        [call("list_bookings"), move_to("10:00"), call("list_bookings"), move_to("11:00")],
        ["move my AC servicing to Saturday 10am", "yes"],
    )
    a = run.a
    assert (run.replies[-1].status, run.replies[-1].kind) == ("NEEDS_INPUT", "slot_conflict")
    ac = lambda: next(b for b in a.env.user_bookings() if b["provider_id"] == "ac_01" and b["status"] == "confirmed")  # noqa: E731
    assert not ac()["slot"].startswith("2026-11-07")
    run.say("how about 11 instead")
    run.say("yes")
    assert run.replies[-1].kind == "confirmed" and ac()["slot"] == "2026-11-07T11:00"
    assert a.env.count("reschedule") == 2 and run.facts()["approvals"] == 2
    assert [x["status"] for x in a.env.store.attempts()] == ["FAILED", "CONFIRMED"]


def test_f7_search_unavailable_through_every_retry_is_bounded_and_reported(scenario):
    def tell_user(request):
        assert tool_json(request, "search_providers")["error"] == "unavailable"  # the model SAW the failure
        return say("I couldn't reach the provider search just now. Please try again in a moment.")

    run = scenario(
        "F7", "search_providers is unavailable on all 3 tries",
        "3 tries with backoff (3 s of fake time), no hang; the model reports it; exactly DONE/answer; nothing mutated",
        [search(), tell_user], [],
    )
    a = run.a
    t0 = a.env.clock.monotonic()
    r = run.say(FIND, before=force("search_providers", "unavailable", times=3))
    assert (r.status, r.kind) == ("DONE", "answer")
    assert [d["code"] for d in a.of("tool_error")] == ["unavailable"] * 3
    assert a.env.clock.monotonic() - t0 == 3.0 and a.world_mutations() == [] and run.facts()["approvals"] == 0


def test_f8_model_backend_failure_is_a_clean_tool_failure(scenario):
    def down(request):
        raise ModelError("backend down")

    run = scenario(
        "F8", "the model backend raises an error",
        "TOOL_FAILURE model_error, exactly one final, 0 mutations, nothing hangs", [down], ["find me a plumber"],
    )
    (r,) = run.replies
    assert (r.status, r.kind) == ("TOOL_FAILURE", "model_error")
    assert run.a.of("understand_error")[0]["type"] == "ModelError" and run.a.world_mutations() == []
