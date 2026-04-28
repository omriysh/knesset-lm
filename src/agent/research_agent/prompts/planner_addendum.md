Research-domain planning guidance (Israeli Knesset)
====================================================

The above tools operate over Israeli Knesset data: Members of Knesset (MKs),
parties / factions, committees, bills, plenum votes, and committee-meeting
protocols. When you plan, follow these domain rules in addition to the
generic rules above.

## 1. Resolve named entities BEFORE you use them

The user's question almost always names entities in free text — an MK
("בני גנץ", "Smotrich"), a committee ("ועדת החוקה"), a bill ("חוק
ההסדרים"), or a vote ("ההצבעה על מתווה הגז"). Those strings are NOT
identifiers. Before any step that references them as if they were IDs,
plan a `find_*` step that turns the name into a stable id:

  - person → `find_mk` → `mk_id`
  - committee → `find_committee` → `committee_id`
  - bill → `find_bill` → `bill_id`
  - vote → `find_vote` → `vote_id`

A downstream step that consumes one of these IDs MUST list the
corresponding `find_*` step in its `deps`. Skipping resolution and
embedding a free-text name into an `args_hint` like
`{"mk_id": "Bibi Netanyahu"}` is a planning error — the executor will
reject it and the pre-critic will flag it as PHANTOM_ENTITY.

The only exception is the `query`/`topic` field on discovery tools
(`search_topics`, `search_protocols_keyword`, `get_votes_on_topic`,
etc.): those accept free-text Hebrew or English. Do not pre-resolve
topic words.

## 2. Cite meetings by `meeting_id`

When a step needs to retrieve a specific meeting (its summary, its full
protocol via `deep_dive_meeting`, or its speeches via
`search_protocols_keyword` with a `meeting_ids` filter), the
identifier is the `meeting_id` string returned by `search_topics` or
`get_committee_sessions`. Do not invent meeting handles such as
"the 14 March committee meeting" — plan an upstream step that produces
the `meeting_id`, then `deps` it.

The synthesizer uses `meeting_id` values in its citations through the
evidence store; stable IDs across replans are how follow-up questions
keep linking to the same primary source.

## 3. Cover the full scope of the question

If the user asks about both MKs' opinions AND their voting record on
the same topic, you need at least one step that touches protocols
(`search_topics` / `search_protocols_keyword`) AND at least one step
that touches votes (`get_votes_on_topic`, `get_mk_votes`,
`get_votes_on_topic_by_mk`). A plan that only covers one half is
under-reaching and will be flagged.

Likewise, "what did committee X discuss about Y?" needs both the
committee resolution (`find_committee`) and a topical search inside
that committee's protocols (`search_topics` filtered to its
`committee_ids`, or `search_protocols_keyword` with a `committee_ids`
filter).

## 4. Use deep-dives sparingly and intentionally

`deep_dive_meeting` is your tool, not the executor's. It is expensive
(~minutes per call). Allocate it to the 1–3 meetings that, based on a
prior `search_topics` result, look like the densest evidence sources
for the question. Do NOT plan a deep-dive on a meeting whose ID you
have not first surfaced through a discovery step — `deps` it.

If the question is broad ("מה דעתם של חברי הכנסת על..."), discovery and
fetch tools are usually sufficient; a deep-dive should be the
exception, not the default.

## 5. Knesset number defaults to 25

All schema defaults already set `knesset_num=25` (the current Knesset).
Override only when the user's question explicitly asks about an
earlier Knesset (e.g. "הכנסת ה-23"). When in doubt, omit
`knesset_num` from `args_hint` and let the default apply.
