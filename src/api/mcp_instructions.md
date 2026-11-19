# KnessetLM: Israeli Knesset data (read-only)

Coverage:
- Committee and plenum protocols (query_protocols, get_meeting_attendance): Knessets {protocol_knessets} (knesset_num), meetings from {meeting_date_from} to {meeting_date_to}. Omit knesset_num to search all of them; every row carries its knesset_num. Plenum sessions are meetings of the committee "מליאת הכנסת", with meeting ids like "p2245272". Rows are discussion topics, opinions (an AI summary plus a verbatim `quote`) and transcript speeches.
- Committees (find_committee): Knessets {roster_knesset_from}-{roster_knesset_to}; omit knesset_num for the committees of Knessets {protocol_knessets}, the ones with protocols. The same committee name in two Knessets is two committees (two committee_id values).
- Bills and plenum votes (query_bills, get_bill, query_votes): live from the Knesset API; omit knesset_num to search every Knesset.
- MKs (find_mk): profiles and positions of Knessets {roster_knesset_from}-{roster_knesset_to}; omit knesset_num to search the MKs of Knessets {protocol_knessets} (an mk_id is the same person in every Knesset).
- Parties (find_party): members of a party in one Knesset, {roster_knesset_from}-{roster_knesset_to} (default {roster_default_knesset}); party names differ between Knessets.

Rules:
- Names may be passed directly as mk_id, party or committees: the server resolves them and says how in `warnings`. find_* gives exact values and profiles when you need them.
- Search with Hebrew key words, 1-2 per call. All words must appear (AND), so use one topic per call and separate calls for synonyms.
- Read `hint` (the next step), `diagnostics` (why a filter matched nothing) and `next` (the exact arguments for the next page) in every response.
- query_protocols pages are about {protocols_page_chars} characters of whole rows per scope, and `offset` counts rows: follow `next` for more. Texts are never cut or split; a long speech comes whole.
- Answer only from returned data. Cite committee, date and meeting_id, and link each claim to its row's `url`: it opens the protocol at the speech, with an opinion's quote highlighted. Quote `quote` or speech `text` verbatim; an `opinion` is a summary, never a quote.

Recipes:
- MK X on topic Y: find_mk(query="X"), then query_protocols(query="Y", mk_id="<mk_id>", search_in=["opinions"])
- Meetings that discussed Y: query_protocols(query="Y", search_in=["topics"])
- A meeting's summary, then its transcript: query_protocols(meeting_ids=["<id>"], search_in=["topics", "opinions"]), then query_protocols(meeting_ids=["<id>"], search_in=["speeches"]) and follow `next`
- How MK X voted on Y: query_votes(query="Y", mk_id="<mk_id>")
