"""
retrieval/lemmatize.py

Exposes a single public function: lemmatize(text) -> str.

When config.USE_DICTABERT_LEMMA is False (the default), this is a fast
passthrough: lowercase + strip Hebrew niqqud (U+05B0–U+05C7).

When True, Dicta-BERT is lazy-loaded on first call and kept as a module-level
singleton for the process lifetime.  Loading takes a few seconds and uses
~1 GB RAM; disable the flag if RAM contention with llama-server is an issue.
"""

import re
import unicodedata

import config

# ── niqqud strip ─────────────────────────────────────────────────────────────

# Unicode range U+05B0–U+05C7 covers Hebrew vowel points (niqqud) and cantillation
_NIQQUD_RE = re.compile(r"[ְ-ׇ]")


def _strip_niqqud(text: str) -> str:
    return _NIQQUD_RE.sub("", text)


# ── Dicta-BERT singleton ──────────────────────────────────────────────────────

_pipeline = None  # lazy-loaded transformers NLP pipeline


def _get_pipeline():
    global _pipeline
    if _pipeline is not None:
        return _pipeline

    # Import here so the module is importable without transformers installed
    # when USE_DICTABERT_LEMMA=False.
    from transformers import pipeline as hf_pipeline  # type: ignore

    device = config.DICTABERT_DEVICE
    # Map "cuda" → 0 for pipeline device arg; cpu stays "cpu"
    device_arg = 0 if device == "cuda" else -1

    _pipeline = hf_pipeline(
        "token-classification",
        model=config.DICTABERT_MODEL,
        aggregation_strategy="simple",
        device=device_arg,
    )
    return _pipeline


# ── public API ────────────────────────────────────────────────────────────────


def lemmatize(text: str) -> str:
    """
    Return a lemmatized (or diacritic-stripped lowercase) version of *text*.

    - When config.USE_DICTABERT_LEMMA is False: lowercase + strip niqqud.
    - When True: run Dicta-BERT and reconstruct tokens from entity_group
      'word' spans, falling back to the stripped form if the model fails.
    """
    if not config.USE_DICTABERT_LEMMA:
        return _strip_niqqud(text).lower()

    try:
        pipe = _get_pipeline()
        results = pipe(text)
        # Each result has 'word' (surface) and 'entity_group'.
        # Dicta-BERT seg model returns the base/lemma in 'word' after aggregation.
        tokens = [r["word"] for r in results if r.get("word")]
        if tokens:
            return " ".join(tokens)
    except Exception as exc:
        print(f"[lemmatize] pipeline failed ({exc}); using fallback", flush=True)

    # Fallback
    return _strip_niqqud(text).lower()
