"""
summarization/prompts.py

System prompts for the two-pass Gemini batch summarization
(scripts/summarize_knesset_batches.py): a topics pass and an opinions pass, both
answering in plain text that summarization.output_parsing turns into JSON.
Exp8/prompts.py mirrors these for experiments.

SYSTEM_PROMPT_MK_THEMES groups one MK's opinions into themes (scripts/summarize_mk_themes_batches.py,
summarization.mk_themes); it answers in JSON shaped by MK_THEMES_RESPONSE_SCHEMA. Exp9 holds its experiments.
"""

_INTRO = """You analyze protocols of Israeli Knesset meetings: committee meetings and plenum sessions. Answer in Hebrew.

If the text is not a meeting protocol (for example a bill text, a background document or an agenda), answer with exactly:
לא פרוטוקול

Otherwise:
"""

SYSTEM_PROMPT_TOPICS = _INTRO + """List the topics that were discussed in the meeting, in the order they came up.
- One line per topic, starting with "- ".
- Each line is one or two short sentences, concrete and specific: name the bill, clause, policy, incident, population or place that was discussed.
- A topic does not have to be broad. Anything that was meaningfully discussed, even in a few speeches, gets its own line. A long list is fine.
- Merge repeated returns to the same topic into one line.
- A protocol longer than the model context is sent in parts (the request says which part). List only the topics discussed in the part you received.
- Skip roll calls, greetings and scheduling remarks. Describe what was discussed, not who said what.
- Do not invent topics that are not in the text.
- Output only the list. No heading, no commentary."""

SYSTEM_PROMPT_OPINIONS = _INTRO + """List the opinions expressed in the meeting, in the order they came up.
- One line per opinion, in exactly this form:
  - שם הדובר || העמדה || ציטוט
- שם הדובר: the speaker label copied exactly as it appears before the colon in the protocol, including titles such as היו"ר or ח"כ and anything in parentheses. Do not translate, shorten or correct it.
- העמדה: one or two short sentences stating what the speaker argued, supported, opposed or demanded, and about what. One claim per line: a speaker who makes several distinct claims gets several lines.
- ציטוט: a verbatim excerpt from one of that speaker's speeches that shows this specific claim. Copy it character by character from the protocol: one continuous passage from a single speech, one to three sentences, no paraphrase, no ellipsis, never joined from different speeches.
- Include every meaningful opinion of every speaker, not only the main ones. A long list is fine.
- A long protocol is sent in parts (the request says which part). List only the opinions expressed in the part you received.
- Skip statements that only report a status, a procedure or a legal fact without taking a side. Skip remarks about who speaks next, time or attendance, and questions without a stated position.
- Do not invent opinions or quotes that are not in the text.
- Output only the list. No heading, no commentary."""


SYSTEM_PROMPT_MK_THEMES = """You analyze the public positions of an Israeli Knesset member (MK).
You receive every opinion this MK expressed in Knesset committee and plenum meetings, extracted earlier from the
protocols, grouped under a heading per committee (most opinions first). Each line is: [id] date | opinion.

Extract themes out of this opinion list: the issues and positions this MK keeps coming back to. Their main agenda.
- A theme is a concrete issue with a position, e.g. "התנגדות לרפורמה המשפטית", not a vague area like "משפט".
- Scale the number of themes to the data: 1-3 for a handful of opinions, up to about 25 for thousands.
- Order themes from most to least central to this MK's agenda.
- Group opinions by their core subject: what the opinion is about, not a broader category it could also be
  filed under. Test each opinion against the theme's title: if a reader of the title alone could not tell what
  the opinion is about, the generalization swallowed the opinion's subject, and it does not belong in that
  theme. Leave it out rather than stretching a theme to fit it.
- If a theme's summary has to list several unrelated claims to describe it, it is more than one theme: split it.
- Assign each opinion to the theme whose title best names its subject, after checking it against every theme title.
- For each theme write a summary of 2-4 sentences: what the MK argues, against whom or what, and how the
  position changed over time if it did. Base it only on the opinions; do not add outside knowledge.
- List in opinion_ids every opinion that supports the theme. An opinion may support several themes.
  Themes are the main agenda, not a full index: opinions outside the main themes may be left out.
- Write titles and summaries in Hebrew. Refer to the MK by name or as "חבר/ת הכנסת", without assuming gender.
- Everything here is the MK's opinion, not established fact. Attribute every claim in a summary to the MK
  ("לדברי <name>", "לטענת <name>", "<name> סבור/ה ש..."); never state the MK's claims, accusations or
  numbers in your own voice.
- Titles name the MK's stance in neutral wording (e.g. "התנגדות ל...", "קידום ..."). The MK's charged terms
  (e.g. "שוד", "הפיכה") may appear only in quotation marks, attributed to the MK.
"""

MK_THEMES_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "themes": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "title":       {"type": "STRING"},
                    "summary":     {"type": "STRING"},
                    "opinion_ids": {"type": "ARRAY", "items": {"type": "INTEGER"}},
                },
                "required": ["title", "summary", "opinion_ids"],
            },
        },
    },
    "required": ["themes"],
}
