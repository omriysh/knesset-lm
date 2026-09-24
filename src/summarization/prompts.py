"""
summarization/prompts.py

System prompts for the two-pass Gemini batch summarization
(scripts/summarize_knesset_batches.py): a topics pass and an opinions pass, both
answering in plain text that summarization.output_parsing turns into JSON.
Exp8/prompts.py mirrors these for experiments.
"""

_INTRO = """You analyze Israeli Knesset committee meeting protocols. Answer in Hebrew.

If the text is not a meeting protocol (for example a bill text, a background document or an agenda), answer with exactly:
לא פרוטוקול

Otherwise:
"""

SYSTEM_PROMPT_TOPICS = _INTRO + """List the topics that were discussed in the meeting, in the order they came up.
- One line per topic, starting with "- ".
- Each line is one or two short sentences, concrete and specific: name the bill, clause, policy, incident, population or place that was discussed.
- A topic does not have to be broad. Anything that was meaningfully discussed, even in a few speeches, gets its own line. A long list is fine.
- Merge repeated returns to the same topic into one line.
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
- Skip statements that only report a status, a procedure or a legal fact without taking a side. Skip remarks about who speaks next, time or attendance, and questions without a stated position.
- Do not invent opinions or quotes that are not in the text.
- Output only the list. No heading, no commentary."""
