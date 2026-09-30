# KnessetLM API

> Read-only data on the Israeli Knesset (25th Knesset and on): committee meeting protocols
> (full transcripts, AI-extracted discussion topics, MK positions with verbatim quotes),
> MKs and their positions, committees, parties, bills and plenum votes.
> Sketch — to be merged with the hand-written version.

All endpoints are `GET` with query-string parameters and return JSON
(`?format=md` returns compact markdown). Texts are in Hebrew; send Hebrew key words.

## Rules for agents

- Answer only from data returned by this API. Never quote text the API did not return.
- Cite every claim: committee, date and `meeting_id` (plus `speech_idx` for a speech or quote).
- Opinions are AI summaries of what a speaker said; the `quote` field is verbatim from the
  protocol. Prefer quoting `quote` or speech `text` over paraphrasing `opinion`.
- Read the `hint` field of every response: it says when to page (`offset=`) or rephrase.

## Endpoints

- `/v1/mks?q=<name>`: resolve an MK name to `mk_id`, with party, committee and government positions.
- `/v1/committees?q=<name>`: resolve a committee name; the returned `name` is the `committee` filter.
- `/v1/parties?q=<name>`: party members; the returned `party` is the `party` filter.
- `/v1/protocols?q=<words>&search_in=topics,opinions,speeches&mk_id=&party=&committee=&meeting_id=&date_from=&date_to=&sort=&top_k=&offset=`:
  keyword search over protocols. Scopes: `topics` (meeting discussion topics), `opinions`
  (who said what, with a verbatim quote), `speeches` (the transcript). Default scopes:
  topics, opinions. An empty `q` lists rows instead (newest meetings first).
- `/v1/meetings/<meeting_id>/attendance`: who attended a meeting.
- `/v1/bills?q=<title words>`: bill search by title. `/v1/bills/<bill_id>?include_text=true`: one bill.
- `/v1/votes?q=<title words>&mk_id=`: plenum votes, optionally how one MK voted.
- `/v1/tools`: machine-readable tool schemas (for function calling). `/v1/meta`: data coverage.
- MCP: the same tools over MCP (Streamable HTTP, stateless) at `https://meorav.com/mcp` or
  `https://mcp.meorav.com`, e.g. `claude mcp add --transport http knesset https://mcp.meorav.com`.

## Search tips

- Use 1-3 plain key words in Hebrew; all words must appear. No OR / AND / quotes.
- Spelling variants (ktiv male / haser) are matched automatically.
- Resolve names first: `/v1/mks` → `mk_id`, `/v1/parties` → `party`, `/v1/committees` → `committee`.
- Dates are `YYYY-MM-DD`, inclusive.

## Recipes

- What does MK X think about Y: `/v1/mks?q=X`, then `/v1/protocols?q=Y&mk_id=<id>&search_in=opinions`.
- Which meetings discussed Y: `/v1/protocols?q=Y&search_in=topics`.
- A meeting's summary: `/v1/protocols?meeting_id=<id>&search_in=topics,opinions&top_k=50`.
- Read a meeting's transcript: `/v1/protocols?meeting_id=<id>&search_in=speeches&top_k=50`, page with `offset`.
