"""
summarization/summary_io.py

The one place that knows the on-disk summary shape,
Data/summaries/<knesset>/<committee>/<stem>.json:

    {"is_protocol": bool, "topics": [str],
     "opinions": [{"speaker", "opinion", "quote", "quote_verified"}]}

and the plain-text rendering the agent / citations use.
"""

import json
from pathlib import Path

import config

SUMMARY_SUFFIX = ".json"


def load_summary(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        "is_protocol": bool(data.get("is_protocol", False)),
        "topics":      [str(t) for t in data.get("topics") or []],
        "opinions":    [
            {
                "speaker":        str(o.get("speaker") or ""),
                "opinion":        str(o.get("opinion") or ""),
                "quote":          str(o.get("quote") or ""),
                "quote_verified": bool(o.get("quote_verified", False)),
            }
            for o in data.get("opinions") or []
        ],
    }


def find_summary_path(meeting_id: str) -> Path | None:
    """Glob Data/summaries/**/*_<meeting_id>.json (the stem's trailing part is the session id)."""
    mid = str(meeting_id)
    if not mid.isdigit():
        return None
    root = config.DATA_DIR / "summaries"
    try:
        return next(root.glob(f"**/*_{mid}{SUMMARY_SUFFIX}"), None)
    except Exception as exc:
        print(f"[summary_io] summary glob failed for {mid!r}: {exc}")
        return None


def transcript_path_for_summary(summary_path: Path) -> Path:
    return Path(str(summary_path).replace("summaries", "raw_transcriptions", 1))


def summary_path_for_transcript(transcript_path: Path) -> Path:
    return Path(str(transcript_path).replace("raw_transcriptions", "summaries", 1))


def render_summary_text(
    topics: list[str],
    opinions: list[dict],
    attendance: list[dict] | None = None,
    section: str | None = None,
) -> str:
    """
    Markdown-ish Hebrew text. opinions rows need speaker/opinion/quote; attendance rows
    need name and optionally party. section limits output to topics|opinions|attendance.
    """
    parts: list[str] = []
    if attendance is not None and section in (None, "attendance"):
        names = [f"{a['name']} ({a['party']})" if a.get("party") else a["name"] for a in attendance]
        parts.append("## נוכחים\n" + (", ".join(names) if names else "(לא ידוע)"))
    if section in (None, "topics"):
        parts.append("## נושאים\n" + "\n".join(f"- {t}" for t in topics))
    if section in (None, "opinions"):
        by_speaker: dict[str, list[dict]] = {}
        for o in opinions:
            by_speaker.setdefault(o.get("speaker_name") or o["speaker"], []).append(o)
        lines = ["## עמדות"]
        for speaker, items in by_speaker.items():
            lines.append(f"### {speaker}")
            for o in items:
                quote = f' — "{o["quote"]}"' if o.get("quote") else ""
                lines.append(f"- {o['opinion']}{quote}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
