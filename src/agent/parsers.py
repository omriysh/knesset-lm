"""
parsers.py

JSON-driven labeled-field extractor for LLM node output.

The output_format object in each node's JSON data describes what to extract
and where to put it.  No changes to this file are needed when adding a new
machine with different output formats.

output_format schema
---------------------
{
  "type": "labeled_fields",

  "fields": [
    {
      "label":    str,             # text prefix to match (e.g. "סוכן בשימוש")
      "var":      str,             # context variable to set
      "required": bool,           # warn if missing  (default: false)
      "optional": bool,           # no warn if missing (default: true)
      "fallback": str,             # if extracted value is empty, use this ctx var
      "format":   str,             # apply "{value}" substitution to extracted text
      "default":  str,             # value to set when field is missing (default: omit)
    }
  ],

  "fallback_content": str,         # if NO fields matched, store full content here

  "loop_control": {
    "done_var":     str,           # if non-empty after parsing → loop ends
    "continue_var": str,           # if non-empty after parsing → loop continues
  },

  "conditions": [
    {
      "when_var":   str,           # check this context variable
      "when_value": str,           # if it equals this value …
      "set": {                     # … set these variables
        "var_name": "{{template}} or literal"
      }
    }
  ]
}

Nodes with output_format=null (or missing) produce no context updates.

Usage
-----
    updates = parse_output(content, node["data"].get("output_format"), ctx)
    ctx.update(updates)
"""

from __future__ import annotations

import re
import warnings
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.context import Context


def parse_output(
    content: str,
    output_format: dict | None,
    ctx: "Context",
) -> dict:
    """
    Extract labeled fields from `content` according to `output_format`.

    Returns a dict of {variable_name: value} to merge into Context.
    If output_format is None or empty → returns {}.
    """
    if not output_format:
        return {}

    fmt_type = output_format.get("type", "labeled_fields")
    if fmt_type != "labeled_fields":
        warnings.warn(f"Unknown output_format type {fmt_type!r}; skipping", stacklevel=2)
        return {}

    fields      = output_format.get("fields", [])
    conditions  = output_format.get("conditions", [])
    fb_content  = output_format.get("fallback_content")

    # ── Step 1: extract labeled fields from content ────────────────────────
    extracted = _extract_labeled_fields(content, fields)

    # ── Step 2: apply fallbacks + formats + defaults ───────────────────────
    result: dict = {}
    any_matched = bool(extracted)

    for field in fields:
        var     = field["var"]
        label   = field["label"]
        value   = extracted.get(label)  # may be None if not found

        if value is not None:
            # Apply format if specified (e.g. "\nהערות: {value}")
            fmt = field.get("format")
            if fmt:
                value = fmt.replace("{value}", value)
            result[var] = value
        else:
            # Field was not found — apply fallback or default
            fallback_var = field.get("fallback")
            if fallback_var:
                # Use value from context (including values just extracted this pass)
                fb_val = result.get(
                    # prefer just-extracted var if it was the same name
                    fallback_var,
                    ctx.get(fallback_var, ""),
                )
                if fb_val:
                    result[var] = str(fb_val)
                    continue

            default = field.get("default")
            if default is not None:
                result[var] = default

            if field.get("required") and not field.get("optional"):
                warnings.warn(
                    f"Required output_format field {label!r} not found in node output",
                    stacklevel=2,
                )

    # ── Step 3: fallback_content — use entire content if nothing matched ───
    if fb_content and not any_matched:
        result[fb_content] = content.strip()

    # ── Step 4: conditions ─────────────────────────────────────────────────
    # Merge result into a temporary view of context for condition evaluation
    temp_vars = {**ctx.as_dict(), **result}

    for cond in conditions:
        when_var   = cond.get("when_var", "")
        when_value = cond.get("when_value", "")
        if str(temp_vars.get(when_var, "")) == when_value:
            for k, v in cond.get("set", {}).items():
                # Apply simple {{var}} template substitution
                rendered = re.sub(
                    r"\{\{(\w+)\}\}",
                    lambda m: str(temp_vars.get(m.group(1), "")),
                    v,
                )
                result[k] = rendered
                temp_vars[k] = rendered   # make immediately available to later conditions

    return result


def get_loop_control(output_format: dict | None) -> dict | None:
    """Return the loop_control sub-dict, or None if not defined."""
    if not output_format:
        return None
    return output_format.get("loop_control")


# ── Internal helpers ──────────────────────────────────────────────────────────

def _extract_labeled_fields(
    content: str,
    fields: list[dict],
) -> dict[str, str]:
    """
    Scan content for lines starting with "label:" and extract values.

    Multi-line values are supported: a field's value continues until the next
    line that starts with a known label, or until end of content.

    Returns {label: extracted_value_str} for matched labels.
    """
    if not fields:
        return {}

    # Build a regex that matches any known label at the start of a line.
    # Labels are matched literally (no regex special chars assumed in Hebrew labels,
    # but we escape just in case).
    label_texts = [f["label"] for f in fields]
    label_pattern = "|".join(re.escape(lbl) for lbl in sorted(label_texts, key=len, reverse=True))
    # e.g. "שאלה (פרוטוקולים)|שאלה (עובדתי)|שאלה (דובר)|שאלה|..."

    # Find all label positions in the content
    line_re = re.compile(
        r"^(" + label_pattern + r")\s*:\s*(.*)",
        re.MULTILINE,
    )

    matches = list(line_re.finditer(content))
    if not matches:
        return {}

    result: dict[str, str] = {}
    for i, m in enumerate(matches):
        label = m.group(1)
        # Value starts on the same line as the label
        value_start = m.group(2)
        # Continues until the start of the next label match (or end of string)
        if i + 1 < len(matches):
            value_tail = content[m.end():matches[i + 1].start()]
        else:
            value_tail = content[m.end():]

        full_value = (value_start + value_tail).strip()
        result[label] = full_value

    return result
