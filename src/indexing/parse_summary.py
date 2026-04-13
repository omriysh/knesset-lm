"""
parse_summary.py

Parse a .txt summary file into a flat list of individual bullet items.

Handles all summary formats produced by the pipeline:

  Numbered formats (heading contains a section number):
    ### 2. נושאי הדיון העיקריים   (old)
    ## 2. נושאי הדיון העיקריים    (new)
    **2. נושאי הדיון העיקריים**   (bold)

  Unnumbered markdown headings:
    ## נוכחים ונעדרים
    ## נושאי הדיון העיקריים

  Bullet styles:
    - text / - **bold:** text     (dash list)
    *   text / *   **bold:** text (asterisk list)
    **Name:** text                (old speaker-position format)
    plain prose                   (fallback)

Attendance sections (heading contains נוכח / נעדר / חסר) are always skipped.
The "חוקים והצעות חוק" section terminates parsing.

Returns
-------
list of dicts  {section: int, idx: int, text: str}
"""

import re
from pathlib import Path
from typing import FrozenSet, List, Dict, Optional

# Stop at the laws section (## or # level only — ### appears in content)
_STOP_PATTERN    = re.compile(r"^#{1,2}\s+.*חוק", re.UNICODE)
# Numbered heading: ## N. text  or  **N. text
_NUMBERED_SEC    = re.compile(r"^(?:#{1,4}\s+|\*\*\s*)(\d+)[.\s]")
# Unnumbered markdown heading: one or more # followed by non-digit text
_MD_HEADING      = re.compile(r"^#{1,4}\s+\D")
# Attendance section: any heading containing attendance keywords
_ATTEND_PATTERN  = re.compile(r"נוכח|נעדר|חסר", re.UNICODE)
# Category-only bullet (no body text): "**Category:**"
_CATEGORY_BULLET = re.compile(r"^\*\*[^*:]+:\*\*\s*$")


def parse_summary_bullets(
    summary_path: Path,
    sections_wanted: Optional[FrozenSet[int]] = None,
) -> List[Dict]:
    """
    Parameters
    ----------
    sections_wanted
        Set of section numbers to include, e.g. ``frozenset({2})`` for the main
        subjects section only.  ``None`` returns all non-attendance sections.
    """
    text  = summary_path.read_text(encoding="utf-8")
    lines = text.split("\n")

    # ── Pass 1: split into labelled sections ──────────────────────────────────
    sections: list[tuple[int, str, list[str]]] = []
    current_num: int | None = None
    current_head: str = ""
    current_lines: list[str] = []

    def _flush() -> None:
        if current_num is not None:
            sections.append((current_num, current_head, list(current_lines)))

    for line in lines:
        stripped = line.strip()

        if _STOP_PATTERN.match(stripped):
            break

        m = _NUMBERED_SEC.match(stripped)
        if m:
            _flush()
            current_num   = int(m.group(1))
            current_head  = stripped
            current_lines = []
        elif _MD_HEADING.match(stripped):
            _flush()
            current_num   = (current_num + 1) if current_num is not None else 1
            current_head  = stripped
            current_lines = []
        elif current_num is not None:
            current_lines.append(stripped)

    _flush()

    # ── Pass 2: extract bullets from each content section ─────────────────────
    bullets: List[Dict] = []

    for sec_num, sec_head, sec_lines in sections:
        if _ATTEND_PATTERN.search(sec_head):
            continue
        if sections_wanted is not None and sec_num not in sections_wanted:
            continue

        extracted: list[str] = []

        for line in sec_lines:
            if not line:
                continue
            if line.startswith("- ") or re.match(r"^\*\s", line):
                raw = line[1:].strip()
                if _CATEGORY_BULLET.match(raw):
                    continue
                txt = re.sub(r"\*\*(.+?)\*\*", r"\1", raw)
                if txt:
                    extracted.append(txt)
            elif line.startswith("**") and ":**" in line:
                extracted.append(line)

        # Fallback: use plain prose lines if no bullets were found
        if not extracted:
            for line in sec_lines:
                txt = re.sub(r"\*\*(.+?)\*\*", r"\1", line.strip())
                if txt and not txt.startswith("#"):
                    extracted.append(txt)

        for txt in extracted:
            bullets.append({"section": sec_num, "idx": len(bullets), "text": txt})

    return bullets
