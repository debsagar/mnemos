# Mnemos

Mnemos is a local memory system for AI agents. It stores conversations, extracts dated facts, and keeps links from those facts back to the original messages. An agent can retrieve earlier context and show where a remembered detail came from.

For example, if you change jobs, Mnemos keeps both updates. You can ask about your current job, look up what it knew before the change, or inspect the messages behind an answer.

## How it works

1. Record conversation turns in an append-only ledger.
2. Extract dated facts and link them to their source turns.
3. Search both original messages and extracted facts with SQLite full-text search.
4. Build answers from retrieved context and a compact profile.

The ledger uses [ActiveGraph](https://docs.activegraph.ai), a separate dependency. Mnemos adds the memory extraction, retrieval, source tracing, CLI and browser interface.

## Run locally

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/debsagar/mnemos.git
cd mnemos
uv sync --locked
export OPENROUTER_API_KEY="your-key"
uv run mnemos chat
```

Model calls use your configured provider and may incur charges. Set `MNEMOS_CHAT_MODEL` and `MNEMOS_EXTRACT_MODEL` to choose models, or `MNEMOS_API_URL` for a compatible endpoint.

```bash
uv run mnemos recall "work"              # retrieve dated facts
uv run mnemos why "work"                 # inspect source messages
uv run mnemos asof 2026-06-20 "Where do I work?"
uv run mnemos digest                     # extract facts from pending turns
uv run mnemos serve                      # local browser interface
```

Memory is stored in `~/.mnemos/` by default. Retrieved context is sent to the configured model provider. The browser agent can execute shell commands as your user; keep it on localhost and use it only in a trusted environment. Its visualization loads libraries from public CDNs.

## Check without an API key

```bash
uv run python test_core.py
```

The check uses fake model replies and temporary memory. It covers source links, extraction failures, interrupted exchanges, background digestion and recorded tool calls. It does not measure model quality.

## Scope

This is a research prototype. Extraction and answers can be wrong; preserving a source does not make an extracted fact correct. The search index and profile can be rebuilt from the ledger. Historical benchmark experiments and private conversation databases are not included in this release, so this README makes no benchmark superiority claims.

## License

MIT. Developed by Sagar Deb. ActiveGraph is maintained separately and retains its own license.
