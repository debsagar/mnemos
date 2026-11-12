"""Mnemos MCP server over stdio. Run with python -m mnemos.mcp_server.

MNEMOS_HOME selects the memory directory. Remember uses the configured
model for background extraction; read tools do not need a model API key.
"""
import json
import os
import sys

from .core import Mnemos

M = Mnemos(os.environ.get("MNEMOS_HOME", "~/.mnemos"))

TOOLS = [
    {"name": "remember",
     "description": "Record one exchange (user message + assistant reply) into the append-only ledger. Facts are extracted asynchronously.",
     "inputSchema": {"type": "object", "properties": {
         "user_msg": {"type": "string"},
         "assistant_msg": {"type": "string", "description": "may be empty for a bare note"}},
         "required": ["user_msg"]}},
    {"name": "recall",
     "description": "Retrieve dated memory (facts, tool runs) about a topic. Returns newest-relevant first with dates.",
     "inputSchema": {"type": "object", "properties": {
         "topic": {"type": "string"},
         "asof": {"type": "string", "description": "optional YYYY-MM-DD: only what was KNOWN by this date"}},
         "required": ["topic"]}},
    {"name": "why",
     "description": "Facts about a topic PLUS the verbatim user words each fact was derived from (provenance receipts).",
     "inputSchema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}},
    {"name": "due",
     "description": "Upcoming plans the ledger knows about (date, text), soonest first.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "stats",
     "description": "Ledger stats: events, turns, facts, pending digestion.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def call(name, args):
    if name == "remember":
        M.record_exchange(args["user_msg"], args.get("assistant_msg", ""))
        M.digest_async()
        return "recorded"
    if name == "recall":
        hits = M.recall(args["topic"], asof=args.get("asof"))
        return "\n".join(f"[{h['date']}] ({h['kind']}) {h['text']}" for h in hits) or "no memory on that topic"
    if name == "why":
        out = []
        for h in M.recall(args["topic"]):
            out.append(f"[{h['date']}] ({h['kind']}) {h['text']}")
            for s in M.sources(h["oid"]):
                out.append(f'    receipt: "{s}"')
        return "\n".join(out) or "no memory on that topic"
    if name == "due":
        return "\n".join(f"{d}  {t}" for d, t in M.due()) or "nothing due"
    if name == "stats":
        return json.dumps(M.stats())
    raise ValueError(f"unknown tool {name}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        rid, method = req.get("id"), req.get("method")
        if rid is None:  # notification
            continue
        try:
            if method == "initialize":
                result = {"protocolVersion": "2024-11-05",
                          "capabilities": {"tools": {}},
                          "serverInfo": {"name": "mnemos", "version": "0.1.0"}}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                p = req["params"]
                result = {"content": [{"type": "text",
                                       "text": call(p["name"], p.get("arguments", {}))}]}
            elif method == "ping":
                result = {}
            else:
                print(json.dumps({"jsonrpc": "2.0", "id": rid,
                                  "error": {"code": -32601, "message": f"unknown method {method}"}}), flush=True)
                continue
            print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True)
        except Exception as e:
            print(json.dumps({"jsonrpc": "2.0", "id": rid,
                              "error": {"code": -32000, "message": str(e)}}), flush=True)


if __name__ == "__main__":
    try:
        main()
    finally:
        M.close()
