"""
scrape_mk_photos.py

Parse a static HTML render of the Knesset MK lobby page and download
profile photos to Data/mk_photos/.

Usage:
    python scripts/scrape_mk_photos.py --html path/to/mk_lobby.html

The HTML can be obtained by saving a static render of:
    https://m.knesset.gov.il/mk/apps/mklobby/main/current-knesset-mks/all-current-mks
"""

import argparse
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import config as _cfg

MK_PHOTOS_DIR = _cfg.DATA_DIR / "mk_photos"

_PATTERN = re.compile(
    r'src="(https://fs\.knesset\.gov\.il/globaldocs/MK/\d+/[^"?]+)[^"]*"'
    r'.+?'
    r'class="profile-name">([^<]+)</div>',
    re.DOTALL,
)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; KnessetLM/1.0)",
    "Referer": "https://m.knesset.gov.il/",
}


def _parse(html: str) -> list[tuple[str, str]]:
    results = []
    seen_names = set()
    for m in _PATTERN.finditer(html):
        url  = m.group(1)
        name = m.group(2).strip()
        if name not in seen_names:
            seen_names.add(name)
            results.append((name, url))
    return results


def _download(name: str, url: str, out_dir: Path) -> bool:
    ext  = Path(url).suffix or ".jpeg"
    dest = out_dir / f"{name}{ext}"
    if dest.exists():
        print(f"  skip  {name}  (already saved)")
        return False
    try:
        r = requests.get(url, headers=_HEADERS, timeout=15)
        r.raise_for_status()
        dest.write_bytes(r.content)
        print(f"  saved {name}  ({len(r.content)//1024} KB)  →  {dest.name}")
        return True
    except Exception as exc:
        print(f"  ERROR {name}: {exc}")
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description="Download MK profile photos.")
    ap.add_argument("--html", required=True, type=Path,
                    help="Path to saved static HTML of the MK lobby page")
    ap.add_argument("--delay", type=float, default=0.3,
                    help="Seconds between requests (default 0.3)")
    args = ap.parse_args()

    html = args.html.read_text(encoding="utf-8")
    pairs = _parse(html)
    if not pairs:
        print("No MK entries found — check that the HTML contains rendered profile cards.")
        sys.exit(1)

    print(f"Found {len(pairs)} MKs in HTML.")
    MK_PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

    new_count = 0
    for name, url in pairs:
        downloaded = _download(name, url, MK_PHOTOS_DIR)
        if downloaded:
            new_count += 1
            time.sleep(args.delay)

    print(f"\nDone. {new_count} new photos saved to {MK_PHOTOS_DIR}")


if __name__ == "__main__":
    main()
