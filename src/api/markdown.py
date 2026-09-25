"""?format=md renderings of API responses, for agents that only read page text."""

import json


def _protocol_row(scope: str, row: dict) -> str:
    where = f"[{row.get('meeting_id')}" + (f" #{row['speech_idx']}" if row.get("speech_idx") is not None else "") + "]"
    header = f"{where} {row.get('committee') or ''} · {row.get('date') or ''}"
    if scope == "topics":
        return f"- {header} — {row.get('topic', '')}"
    if scope == "opinions":
        speaker = row.get("speaker_name") or row.get("speaker") or ""
        party = f" ({row['party']})" if row.get("party") else ""
        quote = f" „{row['quote']}”" if row.get("quote") else ""
        return f"- {header} — {speaker}{party}: {row.get('opinion', '')}{quote}"
    return f"- {header} — {row.get('speaker', '')}: {row.get('text', '')}"


def render_markdown(body: dict) -> str:
    lines = [f"# {body['tool']}"]
    if body.get("hint"):
        lines.append(f"> {body['hint']}")
    if body.get("warnings"):
        lines.append(f"> warnings: {', '.join(body['warnings'])}")
    results = body.get("results")
    if body["tool"] == "query_protocols" and isinstance(results, dict):
        for scope, rows in results.items():
            lines.append(f"\n## {scope} ({len(rows)})")
            lines.extend(_protocol_row(scope, row) for row in rows)
    else:
        lines.append("```json\n" + json.dumps(results, ensure_ascii=False, indent=1) + "\n```")
    return "\n".join(lines) + "\n"
