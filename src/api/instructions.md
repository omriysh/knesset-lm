# KnessetLM API

> Read-only data on the Israeli Knesset (protocols from the 24th Knesset on): committee meeting protocols
> (full transcripts, AI-extracted discussion topics, MK positions with verbatim quotes),
> MKs and their positions, committees, parties, bills and plenum votes.

All endpoints are `GET` with query-string parameters and return JSON
(`?format=md` returns compact markdown). Texts are in Hebrew; send Hebrew key words.

## Rules for agents

- Answer only from data returned by this API. Never quote text the API did not return.
- Cite every claim: committee, date and `meeting_id` (plus `speech_idx` for a speech or quote).
- Opinions are AI summaries of what a speaker said; the `quote` field is verbatim from the
  protocol. Prefer quoting `quote` or speech `text` over paraphrasing `opinion`.
- Read `hint` (the next step), `diagnostics` (why a filter matched nothing) and `next` (the exact
  parameters of the next page: for protocols one entry per scope, e.g. `search_in=opinions&offset=40`).

## Endpoints

- `/v1/mks?q=<name>`: resolve an MK name to `mk_id`, with party, committee and government positions
  (MKs of every processed Knesset unless `knesset_num` names one; an `mk_id` is the same person in every Knesset).
- `/v1/committees?q=<name>`: resolve a committee name; the returned `name` is the `committee` filter
  (committees of every processed Knesset unless `knesset_num` names one).
- `/v1/parties?q=<name>&knesset_num=`: party members in one Knesset (default: the latest); the returned `party` is the
  `party` filter. Party names differ between Knessets.
- `/v1/protocols?q=<words>&search_in=topics,opinions,speeches&mk_id=&party=&committee=&meeting_id=&date_from=&date_to=&sort=&offset=&knesset_num=`:
  keyword search over protocols, of every processed Knesset unless `knesset_num` names one (rows carry their `knesset_num`). Scopes: `topics` (meeting discussion topics), `opinions`
  (who said what, with a verbatim quote), `speeches` (the transcript). Default scopes:
  topics, opinions. An empty `q` lists rows instead (newest meetings first). Pages are about 28000
  characters of JSON per scope, whole rows only; there is no page-size parameter. `offset` counts the
  scope's rows (in ranked order), so follow `next`. Texts are never cut or split: a long speech comes
  whole, so a page can run over 28000 characters by one row.
- `/v1/meetings/<meeting_id>/attendance`: who attended a meeting.
- `/v1/bills?q=<title words>&offset=`: bill search by title, all Knessets, pages of 20. `/v1/bills/<bill_id>?include_text=true&offset=`: one bill; its text comes `max_chars` characters at a time
  from the character `offset`, with `text_chars: [start, end, total]`.
- `/v1/votes?q=<title words>&mk_id=&offset=`: plenum votes, optionally how one MK voted, pages of 20.
- `/v1/tools`: machine-readable tool schemas (for function calling). `/v1/meta`: data coverage.
- MCP: the same tools over MCP (Streamable HTTP, stateless) at `https://meorav.com/mcp` or
  `https://mcp.meorav.com`, e.g. `claude mcp add --transport http knesset https://mcp.meorav.com`.

## Search tips

- Use 1-2 plain key words in Hebrew; all words must appear. No OR / AND / quotes; one topic per call.
- Spelling variants (ktiv male / haser) are matched automatically.
- Resolve names first: `/v1/mks` → `mk_id`, `/v1/parties` → `party`, `/v1/committees` → `committee`.
- Dates are `YYYY-MM-DD`, inclusive.

## Recipes

- What does MK X think about Y: `/v1/mks?q=X`, then `/v1/protocols?q=Y&mk_id=<id>&search_in=opinions`.
- Which meetings discussed Y: `/v1/protocols?q=Y&search_in=topics`.
- A meeting's summary: `/v1/protocols?meeting_id=<id>&search_in=topics,opinions`.
- Read a meeting's transcript: `/v1/protocols?meeting_id=<id>&search_in=speeches`, then follow `next`.
