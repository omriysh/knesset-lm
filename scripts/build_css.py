"""
build_css.py

Builds web/static/tailwind.css from the Tailwind classes used in web/templates and web/static
(config: web/tailwind.config.js). Run after adding or changing Tailwind classes; the built file is committed,
so the server needs no build step.

The Tailwind standalone CLI (no Node) is downloaded once into tools/tailwind/.

Usage
-----
    python scripts/build_css.py
    python scripts/build_css.py --watch
"""

import argparse
import platform
import subprocess
import sys
import urllib.request
from pathlib import Path

TAILWIND_VERSION = "3.4.17"
REPO_ROOT = Path(__file__).parent.parent
WEB_DIR = REPO_ROOT / "web"
TOOLS_DIR = REPO_ROOT / "tools" / "tailwind"


def _binary_name() -> str:
    system = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}[platform.system()]
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    return f"tailwindcss-{system}-{arch}" + (".exe" if system == "windows" else "")


def tailwind_binary() -> Path:
    binary = TOOLS_DIR / _binary_name()
    if not binary.exists():
        url = f"https://github.com/tailwindlabs/tailwindcss/releases/download/v{TAILWIND_VERSION}/{binary.name}"
        print(f"Downloading {url}")
        TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(url, binary)
        binary.chmod(0o755)
    return binary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--watch", action="store_true", help="rebuild on every change")
    args = parser.parse_args()
    command = [str(tailwind_binary()), "-c", "tailwind.config.js", "-i", "tailwind.in.css",
               "-o", "static/tailwind.css", "--minify"]
    if args.watch:
        command.append("--watch")
    return subprocess.call(command, cwd=WEB_DIR)


if __name__ == "__main__":
    sys.exit(main())
