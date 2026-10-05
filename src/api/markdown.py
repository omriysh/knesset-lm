"""?format=md renderings of API responses, for agents that only read page text."""

import json


def _row_text(row: dict, field_name: str) -> str:
    return str(row.get(field_name) or "")


def _protocol_row(scope: str, row: dict) -> str:
    where = f"[{row.get('meeting_id')}" + (f" #{row['speech_idx']}" if row.get("speech_idx") is not None else "") + "]"
    header = f"{where} {row.get('committee') or ''} · {row.get('date') or ''}"
    link = f" ([source]({row['url']}))" if row.get("url") else ""
    if scope == "topics":
        return f"- {header} — {_row_text(row, 'topic')}{link}"
    if scope == "opinions":
        speaker = row.get("speaker_name") or row.get("speaker") or ""
        party = f" ({row['party']})" if row.get("party") else ""
        quote = f" „{_row_text(row, 'quote')}”" if row.get("quote") else ""
        return f"- {header} — {speaker}{party}: {_row_text(row, 'opinion')}{quote}{link}"
    return f"- {header} — {row.get('speaker', '')}: {_row_text(row, 'text')}{link}"


def _next_page_line(next_page: dict) -> str:
    if all(isinstance(patch, dict) for patch in next_page.values()):
        patches = [", ".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in patch.items())
                   for patch in next_page.values()]
        return "> next: " + " | ".join(patches)
    return "> next: " + ", ".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in next_page.items())


def render_markdown(body: dict) -> str:
    lines = [f"# {body['tool']}"]
    if body.get("hint"):
        lines.append(f"> {body['hint']}")
    for diagnostic in body.get("diagnostics") or []:
        suggestions = diagnostic.get("suggestions") or []
        suggestion_text = f" (suggestions: {', '.join(map(str, suggestions))})" if suggestions else ""
        lines.append(f"> diagnostic: {diagnostic.get('message') or diagnostic.get('problem', '')}{suggestion_text}")
    if body.get("next"):
        lines.append(_next_page_line(body["next"]))
    if body.get("warnings"):
        lines.append(f"> warnings: {'; '.join(map(str, body['warnings']))}")
    results = body.get("results")
    if body["tool"] == "query_protocols" and isinstance(results, dict):
        for scope, rows in results.items():
            lines.append(f"\n## {scope} ({len(rows)})")
            lines.extend(_protocol_row(scope, row) for row in rows)
    else:
        lines.append("```json\n" + json.dumps(results, ensure_ascii=False, indent=1) + "\n```")
    return "\n".join(lines) + "\n"
