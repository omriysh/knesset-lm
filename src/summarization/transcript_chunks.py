"""
summarization/transcript_chunks.py

Character spans for the passes of long transcripts: the opinions pass (see config.SUMMARY_OPINIONS_CHUNK_*)
and the topics pass of a transcript longer than the model context. Cut at speaker-turn starts so no speech
is split between two requests.
"""

import bisect
import math

import config
from utils.meeting import speaker_turn_starts


def split_span(transcript: str, start: int, end: int, n_chunks: int,
               turn_starts: list[int] | None = None) -> list[list[int]]:
    """[start, end) cut into n_chunks spans of about equal length, each cut at the speaker-turn
    start nearest its ideal position (a line start when the transcript has no turns there)."""
    if n_chunks <= 1 or end - start < 2:
        return [[start, end]]
    boundaries = [b for b in (turn_starts if turn_starts is not None else speaker_turn_starts(transcript))
                  if start < b < end]
    ideal_length = (end - start) / n_chunks
    edges = [start]
    for chunk_number in range(1, n_chunks):
        ideal = start + round(chunk_number * ideal_length)
        position = bisect.bisect_left(boundaries, ideal)
        nearby = [boundaries[i] for i in (position - 1, position)
                  if 0 <= i < len(boundaries) and boundaries[i] > edges[-1] and abs(boundaries[i] - ideal) <= ideal_length / 2]
        if nearby:
            cut = min(nearby, key=lambda boundary: abs(boundary - ideal))
        else:
            cut = transcript.rfind("\n", edges[-1] + 1, ideal) + 1
            if cut <= edges[-1]:
                cut = ideal
        if edges[-1] < cut < end:
            edges.append(cut)
    edges.append(end)
    return [[edges[i], edges[i + 1]] for i in range(len(edges) - 1)]


def opinion_chunk_spans(transcript: str) -> list[list[int]]:
    """One span covering the transcript, or chunks of about SUMMARY_OPINIONS_CHUNK_TARGET_CHARS
    when it is longer than SUMMARY_OPINIONS_CHUNK_THRESHOLD_CHARS."""
    if len(transcript) <= config.SUMMARY_OPINIONS_CHUNK_THRESHOLD_CHARS:
        return [[0, len(transcript)]]
    n_chunks = math.ceil(len(transcript) / config.SUMMARY_OPINIONS_CHUNK_TARGET_CHARS)
    return split_span(transcript, 0, len(transcript), n_chunks)


def topic_chunk_spans(transcript: str, max_chars: int) -> list[list[int]]:
    """One span covering the transcript, or the fewest chunks of at most about max_chars when it is longer."""
    if len(transcript) <= max_chars:
        return [[0, len(transcript)]]
    return split_span(transcript, 0, len(transcript), math.ceil(len(transcript) / max_chars))
