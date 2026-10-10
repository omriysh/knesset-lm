"""How the subjects data file names a theme. mk_themes.id changes on every themes load, so a theme is named by
its MK and rank, and a fingerprint of its text tells whether it is still the same theme."""

import hashlib


def theme_ref(mk_id: str, rank: int) -> str:
    return f"{mk_id}:{rank}"


def theme_fingerprint(title: str, summary: str) -> str:
    return hashlib.sha1(f"{title}\n{summary}".encode("utf-8")).hexdigest()[:12]


def theme_key(ref: str, fingerprint: str) -> str:
    """A theme's identity including its text: approaches are reused only while these keys stay the same."""
    return f"{ref}#{fingerprint}"
