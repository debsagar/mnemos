"""Mnemos core v0.2: event-sourced ledger + CQRS projections.

Write side (immutable truth):
  ~/.mnemos/ledger.db   append-only event log (ActiveGraph): turns, facts, provenance

Read side (projections — disposable, rebuildable from the ledger at any time):
  ~/.mnemos/index.db    SQLite FTS5 over turns + facts (fast recall, no vector DB)
  ~/.mnemos/profile.md  consolidated who-is-this-user snapshot (human-readable)
  ~/.mnemos/watermark   how far extraction has processed the turn stream

Reply path never waits on extraction: answers are built from profile + FTS
retrieval + recent turns; extraction/consolidation happens in `digest`, which
runs after the reply (or via `mnemos digest`). The watermark makes digestion
at-least-once: a killed process just re-digests from the watermark next time.

Memory design: append-only bi-temporal
facts, no importance filter at extraction, dates resolved at write time,
verbatim receipts via provenance edges, date math in code.
"""

import concurrent.futures, json, os, re, sqlite3, threading, time, traceback, urllib.request
from datetime import date as _date, datetime

EXTRACT_MODEL = os.environ.get("MNEMOS_EXTRACT_MODEL", "qwen/qwen-2.5-7b-instruct")
CHAT_MODEL = os.environ.get("MNEMOS_CHAT_MODEL", "qwen/qwen3.5-27b")
API_URL = os.environ.get("MNEMOS_API_URL", "https://openrouter.ai/api/v1/chat/completions")

EXTRACT = (
    "From this chat exchange dated {date}, extract EVERY fact about the user or their "
    "world: personal details, numbers, names, places, events, changes, plans, preferences. "
    "Do not judge importance — mundane facts matter later. One fact per line: a kind "
    "(identity, state, event, preference, or plan), a date, and the fact. Examples of "
    "the EXACT format:\n"
    "state | 2024-01-15 | works at ExampleCo as a designer\n"
    "event | 2024-01-14 | moved to a new apartment\n"
    "preference | 2024-01-15 | prefers metric units (\"use km please\")\n"
    "plan | 2024-02-20 | dentist appointment\n"
    "(examples only — never copy their content, only their shape) "
    "If the user corrects or denies something ('I wasn't', 'actually no'), state the "
    "correction explicitly as a fact ('never was …', 'no longer …') plus the "
    "corrected truth. "
    "Resolve every relative date ('yesterday', 'next week') into an absolute date using "
    "{date}. If the exchange reveals nothing about the user or their world, output "
    "nothing.\n\n{exchange}"
)

PROFILE = (
    "You maintain a compact profile card for a personal assistant's user. Rewrite the "
    "card by merging the new facts into the current card. Keep it under 300 words, "
    "markdown, sections: Identity / Current / Preferences / Ongoing. Newest information "
    "wins on conflicts; drop superseded values (the full history lives elsewhere). "
    "If a new fact denies or corrects an earlier item ('never was', 'no longer', 'was "
    "wrong'), DELETE that item from the card — do not keep both versions. "
    "Keep dates on time-sensitive items. Output ONLY the card markdown — no code "
    "fences, no commentary.\n\nCURRENT CARD:\n{card}\n\nNEW FACTS:\n{facts}"
)

ANSWER = (
    "You are Mnemos, a personal assistant with an event-sourced long-term memory of "
    "everything this user has told you. Today: {today}.\n"
    "Age annotations on memories are computed for you — trust them, never do your own "
    "date math. Newer facts supersede older ones. If memory doesn't contain the answer, "
    "say so plainly rather than guessing.\n\n"
    "{profile}{memory}{history}User: {msg}\nMnemos:"
)


def llm(prompt, model, max_tokens=800, temperature=0.3):
    key = os.environ.get("OPENROUTER_API_KEY") or _keyfile()
    body = json.dumps({"model": model, "temperature": temperature, "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(API_URL, body,
                                 {"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    for attempt in range(4):
        try:
            resp = json.load(urllib.request.urlopen(req, timeout=120))
            return resp["choices"][0]["message"].get("content") or ""
        except Exception:
            if attempt == 3: raise
            time.sleep(2 ** attempt)


def _keyfile():
    p = os.path.expanduser("~/.mnemos/key")
    if os.path.exists(p): return open(p).read().strip()
    raise SystemExit("Set OPENROUTER_API_KEY or put a key in ~/.mnemos/key")


def _words(text):
    return re.findall(r"[a-zA-Z0-9]{2,}", text.lower())


class Mnemos:
    def __init__(self, home="~/.mnemos"):
        self.home = os.path.expanduser(home)
        os.makedirs(self.home, exist_ok=True)
        # ActiveGraph's store pins its SQLite connection to the thread that
        # created it, so ALL ledger writes run on this one dedicated thread —
        # callers anywhere (HTTP handlers, digest threads, CLI) route through
        # on_ledger(). Root-cause fix for cross-thread ProgrammingError.
        self._exec = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="mnemos-ledger")
        self._ledger_tid = self._exec.submit(threading.get_ident).result()
        self._exec.submit(self._open_runtime).result()
        self.ix = sqlite3.connect(f"{self.home}/index.db", check_same_thread=False)
        self.ix.execute("PRAGMA journal_mode=WAL")
        self.ix.execute("PRAGMA busy_timeout=5000")
        self.ix.execute("CREATE VIRTUAL TABLE IF NOT EXISTS mem USING fts5"
                        "(oid UNINDEXED, kind, date UNINDEXED, learned UNINDEXED, text)")

    def _open_runtime(self):
        from activegraph import Runtime, Graph
        db = f"sqlite:///{self.home}/ledger.db"
        rid = f"{self.home}/run_id"
        if os.path.exists(rid) and os.path.exists(f"{self.home}/ledger.db"):
            self.rt = Runtime.load(db, run_id=open(rid).read().strip(),
                                   behaviors=[], replay_strict=True)
        else:
            self.rt = Runtime(Graph(), behaviors=[], persist_to=db)
            open(rid, "w").write(self.rt.run_id)

    def on_ledger(self, fn, *args):
        """Run fn on the ledger thread, blocking. Reentrant-safe."""
        if threading.get_ident() == self._ledger_tid:
            return fn(*args)
        return self._exec.submit(fn, *args).result()

    def close(self):
        """Flush queued ledger work and stop the ledger thread."""
        self._exec.shutdown(wait=True)
        self.ix.close()

    # ---- write side ---------------------------------------------------------
    def record_exchange(self, user_msg, assistant_msg, when=None):
        """Append verbatim turns to the ledger. Fast path — no LLM calls.
        Returns (user_turn_id, assistant_turn_id) so callers can hang
        provenance edges off the turns (facts, tool runs)."""
        return self.on_ledger(self._record_exchange, user_msg, assistant_msg, when)

    def _record_exchange(self, user_msg, assistant_msg, when=None):
        when = when or datetime.now().strftime("%Y-%m-%d")
        g, ids = self.rt.graph, []
        for role, text in (("user", user_msg), ("assistant", assistant_msg)):
            t = g.add_object("turn", {"role": role, "text": text, "date": when})
            self._index(t.id, "turn", when, when, f"{role}: {text}")
            ids.append(t.id)
        self.rt.run_until_idle()
        self.rt.save_state()
        self.ix.commit()
        return tuple(ids)

    def digest(self):
        """Project pending turns into typed facts + refresh the profile card.
        Runs off the reply path; watermark makes it at-least-once safe.

        The watermark advances per exchange (not per batch), so a crash
        re-extracts at most one exchange. An extractor reply that parses to
        zero facts despite non-empty output is recorded in the ledger as an
        `anomaly` object — a failure must never be indistinguishable from
        "nothing to extract" (lesson from the POMDP project's D6 defect,
        where a silent grader failure was recorded as data).

        Pairing is role-aware, not positional: a dangling user turn left by a
        crash (assistant reply never recorded) is skipped past instead of
        shifting every later pair off by one."""
        return self.on_ledger(self._digest)

    def _digest(self):
        turns = self._turns()
        wm_path = f"{self.home}/watermark"
        wm = int(open(wm_path).read()) if os.path.exists(wm_path) else 0
        pending, g, new_facts = turns[wm:], self.rt.graph, []
        i = 0
        while i < len(pending):
            t_user = pending[i]
            if t_user.data["role"] != "user":
                # stray assistant turn (crash artifact): step past it
                open(wm_path, "w").write(str(wm + i + 1)); i += 1; continue
            if i + 1 >= len(pending):
                break  # dangling user turn at the tail: wait for its reply
            t_asst = pending[i + 1]
            if t_asst.data["role"] != "assistant":
                # user turn whose reply was never recorded: orphan, skip it
                open(wm_path, "w").write(str(wm + i + 1)); i += 1; continue
            when = t_user.data["date"]
            raw = llm(EXTRACT.format(date=when, exchange="\n".join(
                f'{t.data["role"]}: {t.data["text"]}' for t in (t_user, t_asst))),
                EXTRACT_MODEL, temperature=0)
            n_before = len(new_facts)
            for line in raw.splitlines():
                parts = [p.strip() for p in line.split("|", 2)]
                if len(parts) != 3 or not parts[2]:
                    continue
                kind, fdate, text = parts
                if kind.lower() not in ("identity", "state", "event", "preference", "plan"):
                    kind = "state"  # lenient: models sometimes echo the placeholder
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fdate): fdate = when
                # date = event time; learned = ingestion time (bi-temporal)
                f = g.add_object("fact", {"kind": kind.lower(), "date": fdate,
                                          "text": text, "learned": when})
                g.add_relation(f.id, t_user.id, "derived_from", {})
                g.add_relation(f.id, t_asst.id, "derived_from", {})
                self._index(f.id, kind.lower(), fdate, when, text)
                new_facts.append(f"[{fdate}] ({kind}) {text}")
            if raw.strip() and len(new_facts) == n_before:
                a = g.add_object("anomaly", {"date": when, "raw": raw[:500],
                                             "note": "extractor output parsed to zero facts"})
                g.add_relation(a.id, t_user.id, "derived_from", {})
            open(wm_path, "w").write(str(wm + i + 2))
            self.rt.run_until_idle()
            self.rt.save_state()
            self.ix.commit()
            i += 2
        if new_facts:
            card = self._read_profile()
            new_card = llm(PROFILE.format(card=card or "(empty)",
                                          facts="\n".join(new_facts)),
                           EXTRACT_MODEL, temperature=0, max_tokens=500)
            open(f"{self.home}/profile.md", "w").write(new_card.strip())
        return len(new_facts)

    def digest_async(self, on_done=None):
        """Queue a digest on the ledger thread. A crash is PRINTED, never
        swallowed — a silently dead daemon thread is how this bug hid."""
        def _safe():
            try:
                n = self._digest()
                if on_done: on_done(n)
            except Exception:
                traceback.print_exc()
        self._exec.submit(_safe)

    def rebuild_profile(self):
        """The profile card is a projection too: refold it from ALL facts.
        Use after prompt fixes — stale junk in the card doesn't age out on
        its own, because incremental digests only merge NEW facts."""
        facts = sorted(self._facts(), key=lambda f: f.data.get("learned", f.data["date"]))
        if not facts:
            return ""
        lines = "\n".join(f"[{f.data['date']}] ({f.data['kind']}) {f.data['text']}"
                          for f in facts)
        # rare + quality-critical -> chat model; big max_tokens because
        # reasoning models spend budget thinking before the card comes out
        card = llm(PROFILE.format(card="(empty)", facts=lines),
                   CHAT_MODEL, temperature=0, max_tokens=3000).strip()
        open(f"{self.home}/profile.md", "w").write(card)
        return card

    def reindex(self):
        """Projections are disposable: rebuild FTS from the ledger."""
        self.ix.execute("DELETE FROM mem")
        for t in self._turns():
            self._index(t.id, "turn", t.data["date"], t.data["date"],
                        f'{t.data["role"]}: {t.data["text"]}')
        for f in self._facts():
            self._index(f.id, f.data["kind"], f.data["date"],
                        f.data.get("learned", f.data["date"]), f.data["text"])
        self.ix.commit()

    def _index(self, oid, kind, date, learned, text):
        self.ix.execute("INSERT INTO mem VALUES (?,?,?,?,?)",
                        (oid, kind, date, learned, text))

    # ---- read side ----------------------------------------------------------
    def retrieve(self, query, k=10, asof=None, kinds=None):
        ws = _words(query)
        if not ws: return []
        rows = self.ix.execute(
            "SELECT oid, kind, date, learned, text, bm25(mem) FROM mem WHERE mem MATCH ? "
            "ORDER BY bm25(mem) LIMIT 50", (" OR ".join(ws),)).fetchall()
        # Recency-weighted ranking; ties prefer newer memories.
        cand = [r for r in rows if not (asof and r[3] > asof)
                and not (kinds and r[1] not in kinds)]
        if cand:
            lo, hi = min(r[3] for r in cand), max(r[3] for r in cand)
            def frac(learned):
                return 0.0 if hi == lo else \
                    (_date.fromisoformat(learned) - _date.fromisoformat(lo)).days / \
                    max((_date.fromisoformat(hi) - _date.fromisoformat(lo)).days, 1)
            # fts5 bm25() is negative, more negative = better -> flip to positive
            cand.sort(key=lambda r: ((1 + frac(r[3])) * (1 + max(-r[5], 0.0)), r[3]),
                      reverse=True)
        out = [{"oid": r[0], "kind": r[1], "date": r[2], "text": r[4]}
               for r in cand[:k]]
        if len(out) < k:
            # recency fallback: lexical search misses paraphrases ("where do I
            # work" shares no words with "data engineer at Acme") — recent facts
            # cover the gap until real retrieval lands
            seen = {h["oid"] for h in out}
            recent = sorted(self._facts(), reverse=True,
                            key=lambda f: f.data.get("learned", f.data["date"]))
            for f in recent:
                if f.id in seen: continue
                if asof and f.data.get("learned", f.data["date"]) > asof: continue
                out.append({"oid": f.id, "kind": f.data["kind"],
                            "date": f.data["date"], "text": f.data["text"]})
                if len(out) == k: break
        return out

    def _age(self, d, today):
        try:
            days = (_date.fromisoformat(today) - _date.fromisoformat(d)).days
        except ValueError:
            return d
        if days == 0: return f"{d} — today"
        return f"{d} — {days} days ago" if days > 0 else f"{d} — in {-days} days"

    def build_context(self, question, history=None, asof=None):
        today = asof or datetime.now().strftime("%Y-%m-%d")
        profile = ""
        if not asof:  # profile card reflects NOW; time travel must use the ledger only
            card = self._read_profile()
            if card: profile = f"PROFILE CARD:\n{card}\n\n"
        hits = self.retrieve(question, k=10, asof=asof)
        memory = ""
        if hits:
            memory = "RELEVANT MEMORY (facts and verbatim conversation snippets):\n" + "\n".join(
                f"[{self._age(h['date'], today)}] ({h['kind']}) {h['text']}" for h in hits) + "\n\n"
            if len(memory) > 8000:  # egress cap: keep head+tail, mark the cut (LongMemEval 400-blowup lesson)
                memory = memory[:6000] + "\n...[memory truncated]...\n" + memory[-1500:]
        hist = ""
        if history:
            hist = "".join(f"User: {u}\nMnemos: {a}\n" for u, a in history[-6:])
        return ANSWER.format(today=today, profile=profile, memory=memory,
                             history=hist, msg=question)

    def ask(self, question, history=None, asof=None, remember=True):
        reply = llm(self.build_context(question, history, asof), CHAT_MODEL).strip()
        if remember and not asof:
            self.record_exchange(question, reply)
        return reply

    def recall(self, topic, asof=None):
        hits = self.retrieve(topic, k=15, asof=asof)
        return [h for h in hits if h["kind"] != "turn"]

    def sources(self, fact_oid):
        # ponytail: activegraph 1.10.0 get_relations() ignores object_id — filter manually
        return [self.rt.graph.get_object(r.target).data["text"]
                for r in self.rt.graph.all_relations()
                if r.source == fact_oid and r.type == "derived_from"
                and self.rt.graph.get_object(r.target).data["role"] == "user"]

    def due(self):
        today = datetime.now().strftime("%Y-%m-%d")
        return sorted((f.data["date"], f.data["text"]) for f in self._facts()
                      if f.data["kind"] == "plan" and f.data["date"] >= today)

    # ---- ledger accessors ---------------------------------------------------
    def _turns(self):
        ts = [o for o in self.rt.graph.all_objects() if o.type == "turn"]
        return sorted(ts, key=lambda t: int(t.id.split("#")[1]))

    def _facts(self):
        return [o for o in self.rt.graph.all_objects() if o.type == "fact"]

    def anomalies(self):
        return [o for o in self.rt.graph.all_objects() if o.type == "anomaly"]

    def tool_runs(self):
        return [o for o in self.rt.graph.all_objects() if o.type == "tool_run"]

    def _read_profile(self):
        p = f"{self.home}/profile.md"
        return open(p).read() if os.path.exists(p) else ""

    def stats(self):
        wm_path = f"{self.home}/watermark"
        wm = int(open(wm_path).read()) if os.path.exists(wm_path) else 0
        turns = self._turns()
        return {"events": len(self.rt.graph.events),
                "turns": len(turns), "facts": len(self._facts()),
                "anomalies": len(self.anomalies()),
                "pending": len(turns) - wm,  # turns not yet digested
                "indexed": self.ix.execute("SELECT count(*) FROM mem").fetchone()[0]}
