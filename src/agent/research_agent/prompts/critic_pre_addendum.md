Research-domain pre-critic checks (Israeli Knesset)
====================================================

When critiquing a Knesset research plan, apply these domain-specific
checks IN ADDITION to the generic failure modes above.

## 1. Named-entity resolution must precede entity-keyed steps

Flag PHANTOM_ENTITY whenever a step's `args_hint` carries a named
entity (person, committee, bill, vote) under an ID-typed key without a
prior `find_*` step in its `deps`:

  - `mk_id`, `mk` → must come from a `find_mk` step earlier in `deps`,
    OR be a numeric/structured ID surfaced by a prior step.
  - `committee_id`, `committee` → must come from `find_committee`.
  - `bill_id`, `bill_name` → must come from `find_bill`.
  - `vote_id` → must come from `find_vote`.
  - `meeting_ids` → must come from `search_topics` or
    `get_committee_sessions`.

Hebrew names embedded directly into ID slots ("בנימין נתניהו",
"ועדת החוקה") are the most common failure shape. Topic / query strings
("ייצוא גז טבעי", "תקציב הביטחון") are NOT entities — do not flag
them.

## 2. No hallucinated names used directly

The planner has only the user's question and prior evidence (if any)
to draw from. Any MK, committee, bill, or vote name that does not
appear verbatim in the user's question OR in the existing evidence
view MUST be flagged: the planner is hallucinating an entity that was
not provided. This is a special case of PHANTOM_ENTITY; surface it
under that tag.

## 3. Coverage check against question scope

If the user's question pairs two domains (e.g. "what MKs SAID and how
they VOTED on topic X"), confirm the plan touches both:

  - Protocol/speech tools: `search_topics`, `search_protocols_keyword`,
    `get_meeting_summary`, `deep_dive_meeting`.
  - Vote tools: `get_votes_on_topic`, `get_mk_votes`,
    `get_votes_on_topic_by_mk`.
  - MK profile tools: `get_mk_profile`, `get_mk_committees`,
    `get_committee_members`.

A plan that only covers one half of a two-domain question is
UNDERREACH; flag it and suggest the missing tool family in `reason`.

## 4. Knesset-number sanity

If the user's question explicitly references an earlier Knesset (e.g.
"בכנסת ה-23"), every step's `args_hint.knesset_num` should match (or
be omitted to inherit the schema default of 25, which is wrong here).
Treat a mismatch as MISSING_DEP-equivalent and surface it in `reason`.

Output the SAME single-JSON-object shape demanded by the generic
critic-pre instructions above (`{"verdict": "...", "reason": "..."}`).
Do not add extra keys.
