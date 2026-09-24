Research-domain planning guidance (Israeli Knesset)
====================================================

The above tools operate over Israeli Knesset data: Members of Knesset (MKs),
parties / factions, committees, bills, plenum votes, and committee-meeting
protocols. When you plan, follow these domain rules in addition to the
generic rules above.

## 1. Resolve named entities BEFORE you use them

The user's question almost always names entities in free text — an MK
("בני גנץ", "סמוטריץ'"), a party ("יש עתיד"), a committee ("ועדת
החוקה") or a bill ("חוק ההסדרים"). Those strings are NOT identifiers.
Any step that references them as if they were IDs has to depend on a
tool that turns the name into a stable id:

  - person → `find_mk` → `mk_id`
  - party → `find_party` → the exact party name (`party` filter)
  - committee → `find_committee` → the exact committee name
    (`committees` filter of `query_protocols`)
  - bill → `query_bills` → `bill_id` (for `get_bill`)

A downstream step that consumes one of these IDs MUST list the
corresponding resolving step in its `deps`. Skipping resolution and
embedding a free-text name into an `args_hint` like
`{"mk_id": "בנימין נתניהו"}` is a planning error — the executor will
reject it and the pre-critic will flag it as PHANTOM_ENTITY.

**`find_mk` already returns the full profile.** Each candidate in its
result carries a `profile` field with: `factions`, `committee_positions`,
`govministries`, `knesset_roles` (PM, Knesset speaker, coalition/opposition
head…) and `is_current` — all filtered to the requested
`knesset_num`. Do NOT plan a separate profile or committee-list fetch
step after `find_mk` — the data is already there.

**`find_committee` already returns the member list.** Each candidate
in its result includes the active members with their roles (chair,
deputy, member). Do NOT plan a separate member-list fetch step after
`find_committee` — the data is already there.

When you are writing args_hint, the task and the expected evidence,
make sure to ALWAYS use the Hebrew names of MKs, committees and laws.
The tools take only Hebrew, an English hint can throw the executor
off and the plan wouldn't work.

## 2. Cite meetings by `meeting_id`

When a step needs a specific meeting (its summary via `query_protocols`
with `meeting_ids` and `search_in=["topics","opinions"]`, its transcript
via `meeting_ids` and `search_in=["speeches"]`, or its attendance via
`get_meeting_attendance`), the identifier is a `meeting_id` string
returned by an earlier `query_protocols` step. Do not invent meeting
handles such as "the 14 March committee meeting" — plan an upstream
step that produces the `meeting_id`, then `deps` it.

The synthesizer uses `meeting_id` values in its citations through the
evidence store; stable IDs across replans are how follow-up questions
keep linking to the same primary source.

## 3. Cover the full scope of the question

If the user asks about both MKs' opinions AND their voting record on
the same topic, you need at least one step that touches protocols
(`query_protocols`) AND at least one step that touches votes
(`query_votes`). A plan that only covers one half is under-reaching and
will be flagged.

Likewise, "what did committee X discuss about Y?" needs both the
committee resolution (`find_committee`) and a topical search inside
that committee's protocols (`query_protocols` with the committee name
in `committees`).

"What does MK X think about Y?" is `find_mk` → `query_protocols` with
`query`=Y, the `mk_id`, and `search_in=["opinions"]` (add "speeches"
when opinions are thin).

## 4. Search with short keyword queries

`query_protocols` is plain keyword search: every query word must appear
in the row. Use one to three discriminative Hebrew words per call and a
separate call per sub-topic; never OR/AND/NOT. When a query returns
nothing, retry with fewer or different words rather than longer ones.
Reading a meeting's transcript (`meeting_ids` + `search_in=["speeches"]`,
paged with `offset`) is expensive in tokens — reserve it for the 1–3
meetings that earlier results show to be the densest evidence.

## 5. Knesset number defaults to 25

All schema defaults already set `knesset_num=25` (the current Knesset).
Override only when the user's question explicitly asks about an
earlier Knesset (e.g. "הכנסת ה-23"). When in doubt, omit
`knesset_num` from `args_hint` and let the default apply.

## 6. `analyze` steps have NO tools

`task_kind: "analyze"` means the executor reasons over already-collected
evidence using its own LLM, with no external tool calls.

- Set `allowed_tools: []` (empty array) on every `analyze` step.
- NEVER put `expand` in `allowed_tools`. `expand` is an executor-internal
  call always available during step execution — it is not a planning-level
  tool and will fail validation if listed in any step's `allowed_tools`.
- NEVER add a final "synthesis" or "summarize all findings" analyze step.
  The agent automatically synthesizes all collected evidence into the final
  answer after the plan completes — a redundant synthesis step wastes tokens
  and produces no new evidence.
