"""Self-check: digest watermark + anomaly recording, no network. `python test_core.py`"""

import os, shutil, tempfile
import mnemos.core as core

home = tempfile.mkdtemp(prefix="mnemos-selftest-")
replies = []  # queued fake extractor outputs
core.llm = lambda prompt, model, **kw: replies.pop(0)

m = core.Mnemos(home)
m.record_exchange("I started at Acme as a data engineer", "Congrats!", when="2026-07-01")
m.record_exchange("blah", "ok", when="2026-07-02")

# exchange 1 extracts fine; exchange 2 returns garbage (unparseable, non-empty)
replies[:] = ["state | 2026-07-01 | works at Acme as a data engineer",
              "I'm sorry, as an AI I cannot extract facts.",
              "profile card"]
n = m.digest()
assert n == 1, n
assert len(m.anomalies()) == 1, "garbage extractor output must be recorded, not silent"
assert int(open(f"{home}/watermark").read()) == 4, "watermark advances past both exchanges"

# re-digest: nothing pending, no duplicate facts, no second anomaly
n = m.digest()
assert n == 0 and len(m._facts()) == 1 and len(m.anomalies()) == 1

# receipts survive the round trip
fid = m._facts()[0].id
assert m.sources(fid) == ["I started at Acme as a data engineer"]

# stats shape the server relies on
s = m.stats()
assert s["pending"] == 0 and s["anomalies"] == 1 and s["turns"] == 4

# agent loop: one tool call, then a final answer; run recorded in the ledger
import mnemos.agent as agent
queue = [
    {"content": "", "tool_calls": [{"id": "c1", "function": {
        "name": "shell", "arguments": '{"command": "echo hi"}'}}]},
    {"content": "It printed hi."},
]
agent.llm_chat = lambda messages, tools=None, **kw: queue.pop(0)
replies.append("")  # digest for the new exchange extracts nothing (empty raw, no anomaly)
out = agent.chat(m, "run echo hi for me")
assert out["reply"] == "It printed hi."
assert out["tool_runs"][0]["output"] == "hi" and out["tool_runs"][0]["exit_code"] == 0
runs = m.tool_runs()
assert len(runs) == 1 and runs[0].data["command"] == "echo hi"
# provenance: tool_run edge points at the user turn that caused it
uid = [t for t in m._turns() if t.data["role"] == "user"][-1].id
assert any(r.source == runs[0].id and r.target == uid and r.type == "derived_from"
           for r in m.rt.graph.all_relations())
# tool runs are memory: FTS finds them
assert any(h["kind"] == "tool_run" for h in m.retrieve("echo hi", k=5))

# server payload builds and cross-links
from mnemos.server import _graph_payload, _node_payload
p = _graph_payload(m)
assert len(p["nodes"]) == 9  # 6 turns + 1 fact + 1 anomaly + 1 tool_run
assert any(n["type"] == "tool_run" for n in p["nodes"])
assert any(l["type"] == "derived_from" for l in p["links"])
assert any(l["type"] == "next" for l in p["links"])
assert _node_payload(m, fid)["receipts"]

# REGRESSION (the browser bug): ledger writes from foreign threads must not
# crash — activegraph pins its sqlite connection; on_ledger routes around it.
import threading
# queue: the still-pending agent exchange extracts nothing, then tea, then profile
replies[:] = ["", "state | 2026-07-03 | likes tea", "card"]
m.record_exchange("I like tea", "Noted", when="2026-07-03")
errors = []
def _foreign():
    try:
        m.digest()
    except Exception as e:
        errors.append(e)
t = threading.Thread(target=_foreign); t.start(); t.join()
assert not errors, errors
assert any("likes tea" in f.data["text"] for f in m._facts())

# REGRESSION (crash artifact): a dangling user turn must not shift later pairs
g = m.rt.graph
m.on_ledger(lambda: (g.add_object("turn", {"role": "user", "text": "orphan — reply never recorded",
                                           "date": "2026-07-04"}),
                     m.rt.run_until_idle(), m.rt.save_state()))
replies[:] = ["state | 2026-07-05 | plays chess", "card"]
m.record_exchange("I play chess", "Nice", when="2026-07-05")
n = m.digest()
assert n == 1 and any("plays chess" in f.data["text"] for f in m._facts()), \
    "orphan turn must be skipped, not paired with the next user turn"
assert int(open(f"{home}/watermark").read()) == len(m._turns())

# digest_async lands on the ledger thread and completes
replies[:] = ["state | 2026-07-06 | owns a bike", "card"]
m.record_exchange("I got a bike", "Cool", when="2026-07-06")
done = threading.Event()
m.digest_async(on_done=lambda k: done.set())
assert done.wait(5), "async digest never completed"
assert any("owns a bike" in f.data["text"] for f in m._facts())

m.close()
shutil.rmtree(home)
print("ok")
