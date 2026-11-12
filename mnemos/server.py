"""Local browser chat and ledger visualization. Chat can execute shell commands.

Serves the bundled frontend and JSON APIs for graph, source and chat views.
"""

import json, os, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote
from .core import Mnemos

_WEB = os.path.join(os.path.dirname(__file__), "web")


class _State:
    """Reloads the ledger when the file changes. ponytail: mtime poll, no inotify."""

    def __init__(self, home):
        self.home = os.path.expanduser(home)
        self.lock = threading.Lock()
        self.chat_lock = threading.Lock()
        self.history = []  # (user, reply) pairs, server-side, single user
        self.m = None
        self.loaded_at = 0.0

    def fresh(self):
        ledger = f"{self.home}/ledger.db"
        with self.lock:
            mtime = os.path.getmtime(ledger) if os.path.exists(ledger) else 0
            if self.m is None or mtime > self.loaded_at:
                if self.m is not None:
                    self.m.close()  # flush its ledger thread BEFORE the new writer exists
                self.m = Mnemos(self.home)
                self.loaded_at = time.time()
            return self.m


def _graph_payload(m):
    nodes, links = [], []
    turns = m._turns()
    for i, t in enumerate(turns):
        nodes.append({"id": t.id, "type": "turn", "role": t.data["role"],
                      "date": t.data["date"],
                      "label": t.data["text"][:80]})
        if i > 0:  # conversation spine: consecutive turns form a thread
            links.append({"source": turns[i - 1].id, "target": t.id, "type": "next"})
    for f in m._facts():
        nodes.append({"id": f.id, "type": "fact", "kind": f.data["kind"],
                      "date": f.data["date"],
                      "learned": f.data.get("learned", f.data["date"]),
                      "label": f.data["text"]})
    for a in m.anomalies():
        nodes.append({"id": a.id, "type": "anomaly", "date": a.data["date"],
                      "label": a.data["note"]})
    for t in m.tool_runs():
        nodes.append({"id": t.id, "type": "tool_run", "date": t.data["date"],
                      "learned": t.data.get("learned", t.data["date"]),
                      "label": f'$ {t.data["command"]}'})
    ids = {n["id"] for n in nodes}
    for r in m.rt.graph.all_relations():
        if r.type == "derived_from" and r.source in ids and r.target in ids:
            links.append({"source": r.source, "target": r.target, "type": "derived_from"})
    return {"nodes": nodes, "links": links, "stats": m.stats(),
            "profile": m._read_profile()}


def _node_payload(m, oid):
    o = m.rt.graph.get_object(oid)
    if o is None:
        return None
    out = {"id": o.id, "type": o.type, **o.data}
    if o.type == "fact":
        out["receipts"] = m.sources(oid)
    return out


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                if self.path == "/api/chat":
                    from . import agent
                    body = json.loads(self.rfile.read(
                        int(self.headers.get("Content-Length", 0))))
                    msg = (body.get("message") or "").strip()
                    if not msg:
                        return self._json({"error": "empty message"}, 400)
                    m = state.fresh()
                    with state.chat_lock:  # one agent turn at a time
                        out = agent.chat(m, msg, history=state.history)
                        state.history.append((msg, out["reply"]))
                        state.history[:] = state.history[-12:]
                        state.loaded_at = time.time()  # our own write, don't reload
                    # digest queues on the ledger thread; bump loaded_at when its
                    # writes land so we don't reload over our own digest
                    m.digest_async(on_done=lambda n: setattr(state, "loaded_at", time.time()))
                    self._json(out)
                else:
                    self._send(404, b"not found", "text/plain")
            except BrokenPipeError:
                pass
            except Exception as e:
                self._json({"error": str(e)}, 500)

        def do_GET(self):
            try:
                if self.path == "/" or self.path == "/index.html":
                    body = open(os.path.join(_WEB, "index.html"), "rb").read()
                    self._send(200, body, "text/html; charset=utf-8")
                elif self.path == "/api/graph":
                    self._json(_graph_payload(state.fresh()))
                elif self.path.startswith("/api/node/"):
                    node = _node_payload(state.fresh(), unquote(self.path[len("/api/node/"):]))
                    self._json(node if node else {"error": "not found"},
                               200 if node else 404)
                else:
                    self._send(404, b"not found", "text/plain")
            except BrokenPipeError:
                pass
            except Exception as e:
                self._json({"error": str(e)}, 500)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # quiet
            pass

    return Handler


def serve(home="~/.mnemos", port=8077, host="127.0.0.1"):
    # localhost by default ON PURPOSE: /api/chat can run shell commands, so
    # exposing this port is remote code execution. Use an SSH tunnel
    # (ssh -L 8077:localhost:8077 vm) or pass --host 0.0.0.0 knowingly.
    state = _State(home)
    srv = ThreadingHTTPServer((host, port), make_handler(state))
    print(f"mnemos serve — http://localhost:{port}  (ledger: {state.home})")
    if host != "127.0.0.1":
        print(f"WARNING: listening on {host} — anyone who can reach this port "
              "can run shell commands as you.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
