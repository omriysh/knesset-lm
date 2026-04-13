"""
extract_dialogs.py

Coherence-based dialog segmentation for Knesset meeting transcripts.

Algorithm (two passes)
----------------------
Pass 1 — coherence boundaries:
  1. Embed each valid speech.
  2. For every adjacent gap, compute cosine similarity between left/right
     window means → coherence signal.
  3. Detect valleys (windowed depth ≥ threshold) as topic-change boundaries.
  4. Merge groups smaller than MIN_DIALOG_SPEECHES into their smaller neighbour.
  5. Score each group against all summary bullets → topic_scores_vec.
  6. Recursively split oversized groups at the deepest valley (≤ MAX_DIALOG_CHARS).

Pass 2 — same-topic merge:
  Run-length encode pass-1 groups by argmax topic → merge adjacent same-topic
  groups into coarser chunks.  Each pass-2 chunk stores a weighted-mean
  topic_scores_vec (consistent index space with pass-1 and L1 bullets).

Returns
-------
dict with keys:
  dialogs            : list[dict]  — pass-2 (coarse) chunks
  raw_dialogs        : list[dict]  — pass-1 (fine) chunks
  coherence_signal   : list[float]
  raw_boundaries     : list[int]   — pass-1 gap indices
  boundaries         : list[int]   — pass-2 gap indices
  speech_topic_assign: list[int]
  speech_topic_scores: list[float]
  valid_idxs         : list[int]
  valid_speeches     : list[dict]
"""

from typing import Any, Dict, List, Optional

import numpy as np

import config


# ── Internal helpers ───────────────────────────────────────────────────────────

def _relu_normalize(vec: np.ndarray) -> np.ndarray:
    v = np.maximum(0.0, vec)
    s = v.sum()
    return v / s if s > 1e-8 else v


def _compute_coherence(speech_embs: np.ndarray, window: int) -> List[float]:
    """
    For each gap j ∈ [0, N-2], cosine similarity between the L2-normalised mean
    of the left block [j-window+1 .. j] and the right block [j+1 .. j+window].
    """
    N = len(speech_embs)
    out = []
    for j in range(N - 1):
        left  = speech_embs[max(0, j - window + 1) : j + 1].mean(axis=0)
        right = speech_embs[j + 1 : j + 1 + window].mean(axis=0)
        left  = left  / (np.linalg.norm(left)  + 1e-8)
        right = right / (np.linalg.norm(right) + 1e-8)
        out.append(float(left @ right))
    return out


def _find_boundaries(
    coherence: List[float],
    depth_threshold: float,
    peak_window: int,
) -> List[int]:
    """
    Return gap indices with a significant topic change.

    Depth is measured against the windowed peak on each side (not just the
    immediate neighbour), so broad gradual valleys are detected reliably.
    Among consecutive candidates, only the deepest point is kept.
    """
    arr = np.array(coherence)
    N   = len(arr)
    candidates = []
    for j in range(N):
        val       = arr[j]
        left_max  = float(arr[max(0, j - peak_window) : j].max())          if j > 0     else float("inf")
        right_max = float(arr[j + 1 : min(N, j + 1 + peak_window)].max())  if j < N - 1 else float("inf")
        if min(left_max, right_max) - val >= depth_threshold:
            candidates.append(j)

    if not candidates:
        return []

    boundaries = []
    run = [candidates[0]]
    for j in candidates[1:]:
        if j == run[-1] + 1:
            run.append(j)
        else:
            boundaries.append(min(run, key=lambda k: arr[k]))
            run = [j]
    boundaries.append(min(run, key=lambda k: arr[k]))
    return boundaries


def _merge_small_groups(groups: List[tuple], min_size: int) -> List[tuple]:
    """Repeatedly merge the first too-small group into its smaller neighbour."""
    groups = list(groups)
    while len(groups) > 1:
        small = [(i, e - s + 1) for i, (s, e) in enumerate(groups) if e - s + 1 < min_size]
        if not small:
            break
        i, _ = small[0]
        s, e = groups[i]
        if i == 0:
            ns, ne = groups[1]
            groups = [(s, ne)] + groups[2:]
        elif i == len(groups) - 1:
            ps, pe = groups[-2]
            groups = groups[:-2] + [(ps, e)]
        else:
            ps, pe = groups[i - 1]
            ns, ne = groups[i + 1]
            if (pe - ps) <= (ne - ns):
                groups = groups[: i - 1] + [(ps, e)] + groups[i + 1 :]
            else:
                groups = groups[:i] + [(s, ne)] + groups[i + 2 :]
    return groups


def _span_chars(groups: list, valid_speeches: tuple, sg_start: int, sg_end: int) -> int:
    gs, ge = groups[sg_start][0], groups[sg_end][1]
    lines = [
        f"{valid_speeches[j]['speaker']}: {valid_speeches[j]['text_he']}"
        for j in range(gs, ge + 1)
    ]
    return len("\n".join(lines))


def _split_super_group(
    sg_start: int,
    sg_end: int,
    groups: list,
    coherence: List[float],
    pass1_boundaries: List[int],
    valid_speeches: tuple,
    max_chars: int,
) -> list:
    """Recursively split an oversized pass-2 chunk at the deepest coherence valley."""
    if _span_chars(groups, valid_speeches, sg_start, sg_end) <= max_chars:
        return [(sg_start, sg_end)]
    if sg_start == sg_end:
        return [(sg_start, sg_end)]
    best_d = min(
        range(sg_start, sg_end),
        key=lambda d: coherence[pass1_boundaries[d]],
    )
    return (
        _split_super_group(sg_start, best_d,     groups, coherence, pass1_boundaries, valid_speeches, max_chars)
        + _split_super_group(best_d + 1, sg_end, groups, coherence, pass1_boundaries, valid_speeches, max_chars)
    )


def _make_dialog_dict(
    groups: list,
    valid_speeches: tuple,
    speeches: List[Dict],
    valid_idxs: tuple,
    bullets: List[Dict],
    gs: int,
    ge: int,
    score_vec: np.ndarray,
) -> Dict[str, Any]:
    sp        = [speeches[valid_idxs[j]] for j in range(gs, ge + 1)]
    full_text = "\n".join(f"{s['speaker']}: {s['text_he']}" for s in sp)
    t_idx     = int(score_vec.argmax())
    return dict(
        topic_idx        = t_idx,
        topic_text       = bullets[t_idx]["text"],
        topic_score      = float(score_vec.max()),
        topic_scores_vec = score_vec.tolist(),
        speeches         = sp,
        full_dialog_text = full_text,
        start_speech_idx = int(valid_idxs[gs]),
        end_speech_idx   = int(valid_idxs[ge]),
        speakers         = list({s["speaker"] for s in sp if s.get("speaker")}),
        char_count       = len(full_text),
        speech_count     = ge - gs + 1,
    )


# ── Public entry point ────────────────────────────────────────────────────────

def extract_dialogs_coherence(
    speeches: List[Dict],
    bullets: List[Dict],
    embedder,
    *,
    window: Optional[int] = None,
    depth_threshold: Optional[float] = None,
    peak_window: Optional[int] = None,
    min_dialog_speeches: Optional[int] = None,
    min_speech_chars: Optional[int] = None,
    precomputed_speech_embs: Optional[np.ndarray] = None,
) -> Dict[str, Any]:
    """
    Parameters
    ----------
    speeches : list of {speaker, text_he} dicts
    bullets  : list of {section, idx, text} dicts from parse_summary_bullets()
    embedder : ProtocolEmbedder instance
    precomputed_speech_embs : optional (N, D) array — skips re-embedding speeches
    """
    window              = window              or config.COHERENCE_WINDOW
    depth_threshold     = depth_threshold     or config.COHERENCE_DEPTH_THRESHOLD
    peak_window         = peak_window         or config.COHERENCE_PEAK_WINDOW
    min_dialog_speeches = min_dialog_speeches or config.MIN_DIALOG_SPEECHES
    min_speech_chars    = min_speech_chars    or config.MIN_SPEECH_CHARS

    _empty: Dict[str, Any] = dict(
        dialogs=[], raw_dialogs=[], coherence_signal=[], raw_boundaries=[],
        boundaries=[], speech_topic_assign=[], speech_topic_scores=[],
        valid_idxs=[], valid_speeches=[],
    )

    if not bullets or not speeches:
        return _empty

    valid = [
        (i, s)
        for i, s in enumerate(speeches)
        if s.get("speaker", "").strip() and len(s.get("text_he", "")) >= min_speech_chars
    ]
    if not valid:
        return _empty

    valid_idxs, valid_speeches = zip(*valid)
    N = len(valid_speeches)
    B = len(bullets)

    speech_texts = [f"{s['speaker']}: {s['text_he']}" for s in valid_speeches]
    if precomputed_speech_embs is not None and len(precomputed_speech_embs) == N:
        speech_embs = precomputed_speech_embs
    else:
        speech_embs = embedder.embed(speech_texts, embedder.INSTR_ASSIGN)

    coherence = _compute_coherence(speech_embs, window)

    raw_boundaries = _find_boundaries(coherence, depth_threshold, peak_window)
    cut_pts  = sorted(set(raw_boundaries))
    g_starts = [0] + [j + 1 for j in cut_pts]
    g_ends   = cut_pts + [N - 1]
    groups   = [(s, e) for s, e in zip(g_starts, g_ends) if e >= s]
    groups   = _merge_small_groups(groups, min_dialog_speeches)
    pass1_boundaries = [groups[i][1] for i in range(len(groups) - 1)]

    bullet_texts = [b["text"] for b in bullets]
    bullet_embs  = embedder.embed(bullet_texts, embedder.INSTR_ASSIGN)

    group_texts = [
        "\n".join(
            f"{valid_speeches[j]['speaker']}: {valid_speeches[j]['text_he']}"
            for j in range(gs, ge + 1)
        )
        for gs, ge in groups
    ]
    group_embs       = embedder.embed(group_texts, embedder.INSTR_ASSIGN)
    sim_gb           = group_embs @ bullet_embs.T
    score_vecs_norm  = np.apply_along_axis(_relu_normalize, 1, sim_gb)
    topic_idxs       = score_vecs_norm.argmax(axis=1).tolist()

    G = len(groups)

    pass1_dialogs: List[Dict] = [
        _make_dialog_dict(groups, valid_speeches, speeches, valid_idxs, bullets,
                          gs, ge, score_vecs_norm[g_i])
        for g_i, (gs, ge) in enumerate(groups)
    ]

    # Pass-2: run-length merge of adjacent same-topic pass-1 groups
    super_groups: List[tuple] = []
    start_d   = 0
    cur_topic = topic_idxs[0]
    for d in range(1, G):
        if topic_idxs[d] != cur_topic:
            super_groups.append((start_d, d - 1))
            start_d   = d
            cur_topic = topic_idxs[d]
    super_groups.append((start_d, G - 1))

    # Split oversized chunks at the deepest coherence valley (recursive)
    if config.MAX_DIALOG_CHARS > 0:
        split: List[tuple] = []
        for sg in super_groups:
            split.extend(
                _split_super_group(
                    sg[0], sg[1], groups, coherence,
                    pass1_boundaries, valid_speeches,
                    config.MAX_DIALOG_CHARS,
                )
            )
        super_groups = split

    pass2_boundaries = [
        pass1_boundaries[sg[0] - 1] for sg in super_groups[1:]
    ]

    pass2_dialogs: List[Dict] = []
    speech_topic_assign = [0]   * N
    speech_topic_scores = [0.0] * N

    for sg_start, sg_end in super_groups:
        gs = groups[sg_start][0]
        ge = groups[sg_end][1]
        sizes   = np.array(
            [groups[d][1] - groups[d][0] + 1 for d in range(sg_start, sg_end + 1)],
            dtype=float,
        )
        weights    = sizes / sizes.sum()
        merged_vec = sum(
            score_vecs_norm[d] * w
            for d, w in zip(range(sg_start, sg_end + 1), weights)
        )
        merged_vec = _relu_normalize(merged_vec)
        dlg = _make_dialog_dict(
            groups, valid_speeches, speeches, valid_idxs, bullets,
            gs, ge, merged_vec,
        )
        pass2_dialogs.append(dlg)
        for j in range(gs, ge + 1):
            speech_topic_assign[j] = dlg["topic_idx"]
            speech_topic_scores[j] = dlg["topic_score"]

    return dict(
        dialogs             = pass2_dialogs,
        raw_dialogs         = pass1_dialogs,
        coherence_signal    = coherence,
        raw_boundaries      = pass1_boundaries,
        boundaries          = pass2_boundaries,
        speech_topic_assign = speech_topic_assign,
        speech_topic_scores = speech_topic_scores,
        valid_idxs          = list(valid_idxs),
        valid_speeches      = list(valid_speeches),
    )
