"""The agent loop: chat with tools, every tool run recorded in the ledger.

The chat model can run shell commands on this machine. Each run becomes a
`tool_run` object with a derived_from edge to the user turn that caused it —
so `why` and the 3D view can show what the agent DID, not just what was said.
Actions are memory too: tool runs are FTS-indexed, so "what did you check
yesterday?" retrieves them like any fact.
"""

import json, os, subprocess, time, urllib.request
from datetime import datetime
from .core import CHAT_MODEL, API_URL, _keyfile

MAX_TOOL_CALLS = 8
TOOL_TIMEOUT = 60
OUTPUT_CAP = 8000

TOOLS = [{
    "type": "function",
    "function": {
        "name": "shell",
        "description": "Run a bash command on the user's VM (their personal machine — "
                       "you are their assistant and have their permission). Output is "
                       f"truncated to {OUTPUT_CAP} chars. cwd is the user's home.",
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "string"}},
                       "required": ["command"]},
    },
}]

SYSTEM = (
    "You are Mnemos, a personal assistant living on the user's VM with an event-sourced "
    "long-term memory and a shell tool. Today: {today}.\n"
    "Age annotations on memories are computed for you — trust them, never do your own "
    "date math. Newer facts supersede older ones. If memory doesn't contain an answer, "
    "say so plainly rather than guessing.\n"
    "Use the shell tool when the user asks you to look at or do something on the machine. "
    "Prefer looking before acting; keep commands simple; never run anything destructive "
    "unless the user explicitly asked for exactly that.\n\n{profile}{memory}"
)


def llm_chat(messages, tools=None, max_tokens=2500):
    key = os.environ.get("OPENROUTER_API_KEY") or _keyfile()
    payload = {"model": CHAT_MODEL, "max_tokens": max_tokens, "temperature": 0.3,
               "messages": messages}
    if tools:
        payload["tools"] = tools
    req = urllib.request.Request(API_URL, json.dumps(payload).encode(),
                                 {"Authorization": f"Bearer {key}",
                                  "Content-Type": "application/json"})
    for attempt in range(4):
        try:
            resp = json.load(urllib.request.urlopen(req, timeout=180))
            return resp["choices"][0]["message"]
        except Exception:
            if attempt == 3: raise
            time.sleep(2 ** attempt)


def run_shell(command):
    try:
        p = subprocess.run(["bash", "-lc", command], capture_output=True, text=True,
                           timeout=TOOL_TIMEOUT, cwd=os.path.expanduser("~"))
        out = (p.stdout + p.stderr).strip()
        return p.returncode, out[:OUTPUT_CAP] + ("\n...[truncated]" if len(out) > OUTPUT_CAP else "")
    except subprocess.TimeoutExpired:
        return -1, f"[timed out after {TOOL_TIMEOUT}s]"


def chat(m, user_msg, history=None):
    """One agent turn. Returns {"reply", "tool_runs"}; writes everything to the ledger."""
    today = datetime.now().strftime("%Y-%m-%d")
    card = m._read_profile()
    hits = m.retrieve(user_msg, k=10)
    memory = ""
    if hits:
        memory = "RELEVANT MEMORY:\n" + "\n".join(
            f"[{m._age(h['date'], today)}] ({h['kind']}) {h['text']}" for h in hits) + "\n"
        if len(memory) > 8000:
            memory = memory[:6000] + "\n...[memory truncated]...\n" + memory[-1500:]
    messages = [{"role": "system", "content": SYSTEM.format(
        today=today, memory=memory,
        profile=f"PROFILE CARD:\n{card}\n" if card else "")}]
    for u, a in (history or [])[-6:]:
        messages += [{"role": "user", "content": u}, {"role": "assistant", "content": a}]
    messages.append({"role": "user", "content": user_msg})

    tool_runs = []
    for _ in range(MAX_TOOL_CALLS):
        msg = llm_chat(messages, tools=TOOLS)
        calls = msg.get("tool_calls")
        if not calls:
            reply = (msg.get("content") or "").strip() or "(no reply)"
            break
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": calls})
        for c in calls:
            try:
                cmd = json.loads(c["function"]["arguments"]).get("command", "")
            except (json.JSONDecodeError, KeyError):
                cmd = ""
            code, out = run_shell(cmd) if cmd else (-1, "[no command given]")
            tool_runs.append({"command": cmd, "exit_code": code, "output": out})
            messages.append({"role": "tool", "tool_call_id": c.get("id", ""),
                             "content": out or "(no output)"})
    else:
        reply = "(stopped: hit the tool-call limit)"

    # ledger: turns first, then tool_run objects hanging off the user turn.
    # One unit of work on the ledger thread (activegraph is thread-pinned).
    def _write():
        uid, _ = m.record_exchange(user_msg, reply, when=today)
        g = m.rt.graph
        for tr in tool_runs:
            o = g.add_object("tool_run", {"date": today, "learned": today,
                                          "command": tr["command"],
                                          "exit_code": tr["exit_code"],
                                          "output": tr["output"][:1000]})
            g.add_relation(o.id, uid, "derived_from", {})
            m._index(o.id, "tool_run", today, today,
                     f'ran `{tr["command"]}` -> {tr["output"][:300]}')
        if tool_runs:
            m.rt.run_until_idle()
            m.rt.save_state()
            m.ix.commit()
    m.on_ledger(_write)
    return {"reply": reply, "tool_runs": tool_runs}
