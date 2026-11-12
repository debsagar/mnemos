"""mnemos CLI: chat, ask, recall, why, asof, digest, due, profile, reindex, log, stats."""

import argparse, json, sys
from .core import Mnemos


def main():
    p = argparse.ArgumentParser(prog="mnemos",
                                description="Agent memory with dated facts and source-linked history.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("chat", help="interactive session with memory")
    a = sub.add_parser("ask", help="one-shot question"); a.add_argument("question")
    r = sub.add_parser("recall", help="dated fact timeline for a topic"); r.add_argument("topic")
    w = sub.add_parser("why", help="facts + the verbatim words they came from")
    w.add_argument("topic")
    o = sub.add_parser("asof", help="time travel: answer from what was KNOWN on DATE")
    o.add_argument("date", metavar="YYYY-MM-DD"); o.add_argument("question")
    sub.add_parser("digest", help="project pending turns into facts + refresh profile")
    sub.add_parser("due", help="upcoming plans the ledger knows about")
    pr = sub.add_parser("profile", help="show the consolidated profile card")
    pr.add_argument("--rebuild", action="store_true",
                    help="refold the card from ALL facts in the ledger")
    sub.add_parser("reindex", help="rebuild the FTS projection from the ledger")
    sub.add_parser("log", help="tail the raw event ledger")
    sub.add_parser("stats", help="ledger + index size")
    sub.add_parser("anomalies", help="extraction failures recorded in the ledger")
    s = sub.add_parser("serve", help="chat + 3D ledger view in the browser")
    s.add_argument("--port", type=int, default=8077)
    s.add_argument("--host", default="127.0.0.1",
                   help="DANGER: non-localhost exposes shell access")
    args = p.parse_args()

    if args.cmd == "serve":
        from .server import serve
        serve(port=args.port, host=args.host)
        return

    m = Mnemos()

    if args.cmd == "chat":
        print("mnemos — I remember. (ctrl-d to leave)")
        m.digest_async()  # catch up on anything a previous session left pending
        history = []
        while True:
            try:
                msg = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print(); break
            if not msg: continue
            reply = m.ask(msg, history=history)
            print("mnemos>", reply)
            history.append((msg, reply))
            m.digest_async()  # extraction off the reply path
    elif args.cmd == "ask":
        print(m.ask(args.question))
        m.digest()  # one-shot process: digest synchronously before exit
    elif args.cmd == "asof":
        print(m.ask(args.question, asof=args.date, remember=False))
    elif args.cmd in ("recall", "why"):
        hits = m.recall(args.topic)
        if not hits: print("nothing in the ledger about that."); return
        for h in hits:
            print(f"[{h['date']}] ({h['kind']}) {h['text']}")
            if args.cmd == "why":
                for s in m.sources(h["oid"]): print(f'    receipt: "{s}"')
    elif args.cmd == "digest":
        print(f"projected {m.digest()} new facts")
    elif args.cmd == "due":
        for d, t in m.due(): print(f"[{d}] {t}")
    elif args.cmd == "profile":
        if args.rebuild:
            print(m.rebuild_profile() or "(no facts in the ledger yet)")
        else:
            print(m._read_profile() or "(no profile yet — run `mnemos digest`)")
    elif args.cmd == "reindex":
        m.reindex(); print(json.dumps(m.stats()))
    elif args.cmd == "log":
        for e in m.rt.graph.events[-20:]:
            print(json.dumps({"id": e.id, "type": e.type,
                              "payload": str(e.payload)[:100]}))
    elif args.cmd == "stats":
        print(json.dumps(m.stats()))
    elif args.cmd == "anomalies":
        for a in m.anomalies():
            print(f'[{a.data["date"]}] {a.data["note"]}: {a.data["raw"][:120]!r}')


if __name__ == "__main__":
    sys.exit(main())
