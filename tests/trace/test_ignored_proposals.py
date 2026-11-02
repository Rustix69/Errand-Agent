"""An ignored proposal is visible in the trace (and its reason in the viewer), so a real-model run shows it."""

from demo.web.server import trace_class
from errand.model import calls
from errand.trace import read_trace, trace_path
from errand.trace.cli import summary
from tests.conftest import WEEK
from tests.evals.scripts import pick


def test_an_ignored_proposal_is_in_the_trace_with_its_reason_and_stands_out_in_the_web_pills(build):
    blind = ("propose_booking", {"provider_id": "pl_02", "slot": "2026-11-03T10:00"})
    read = ("search_providers", {"service": "plumber", "area": "Indiranagar", "date_range": WEEK})
    a, tracer = build([calls(blind, read), pick()])
    r = a.say("find a plumber in Indiranagar this week")
    rows = read_trace(trace_path(tracer.dir, r.run_id))
    (ignored,) = [x for x in rows if x["event"] == "tool_call_ignored"]
    assert ignored["data"]["tool"] == "propose_booking" and ignored["data"]["reason"] == "read_results_unseen"
    assert "read_results_unseen" in summary(ignored) and "propose_booking" in summary(ignored)  # what `show` / `follow` print
    order = [x["event"] for x in rows]
    assert order.index("tool_call_ignored") < order.index("propose_requested")  # ignored first, honoured on the next turn
    assert trace_class("tool_call_ignored", ignored["data"]) == "bad"  # a red pill in the web UI
    assert [x["data"]["turn"] for x in rows if x["event"] == "model_call"] == [1, 2]
