# audio/_stitch — concatenate per-segment transcripts with overlap dedup.
#
# Why this exists:
#   When VAD finds a clean silence gap, adjacent audio segments don't overlap,
#   so adjacent transcripts naturally concatenate.  But when no silence is
#   found in the [min_seg, max_seg] window we hard-cut with a small overlap
#   (default 2.5s) so a sentence split mid-word still appears intact in at
#   least one segment.  This module reconciles those overlaps in the text
#   domain: find the longest common substring at segment N's tail vs
#   segment N+1's head; if it's long enough, splice at that point.
#
# Design notes:
#   - Pure stdlib (``difflib.SequenceMatcher``).  Works for both Chinese
#     (character-level matching) and English (still character-level — words
#     and spaces just become longer matches).
#   - When no overlap is found we fall back to plain join with a single space
#     separator.  We deliberately accept a small risk of duplicated content
#     over silently dropping content.
#   - Tail/head probe windows are bounded (default 200 chars each) so cost
#     stays O(N * w²) in the worst case rather than O(text_len²).

from __future__ import annotations

from difflib import SequenceMatcher

# Default tuning — exposed as kwargs but rarely overridden.
DEFAULT_PROBE_CHARS = 200
DEFAULT_MIN_OVERLAP_CHARS = 20


def _splice_pair(
    left: str,
    right: str,
    *,
    probe_chars: int,
    min_overlap_chars: int,
) -> str:
    """Splice two consecutive transcripts, removing detected overlap.

    Looks for the longest common substring between ``left[-probe_chars:]`` and
    ``right[:probe_chars]``.  When the match is at least *min_overlap_chars*
    long, the overlapping text is kept once (taken from *left*) and the
    portion of *right* before the match end is dropped.

    Returns the spliced text.  When no sufficient overlap is found, the two
    are concatenated with a single space separator.
    """
    if not left:
        return right
    if not right:
        return left

    tail = left[-probe_chars:]
    head = right[:probe_chars]

    matcher = SequenceMatcher(a=tail, b=head, autojunk=False)
    match = matcher.find_longest_match(0, len(tail), 0, len(head))

    if match.size < min_overlap_chars:
        # No reliable overlap — plain concat. The space separator is safe for
        # CJK (will be visible but harmless) and natural for Latin scripts.
        return f"{left} {right}".strip()

    # Keep left intact (it contains the overlap up to tail end-of-match).
    # Drop the portion of right up to and including the matched substring.
    drop_until = match.b + match.size
    return left + right[drop_until:]


def stitch_transcripts(
    texts: list[str],
    *,
    probe_chars: int = DEFAULT_PROBE_CHARS,
    min_overlap_chars: int = DEFAULT_MIN_OVERLAP_CHARS,
) -> str:
    """Concatenate per-segment transcripts, deduplicating any overlap.

    Args:
        texts: per-segment ASR outputs in playback order.  Empty / None-ish
            entries are skipped.
        probe_chars: how many trailing/leading characters to inspect on each
            side when searching for overlap.  Larger probes catch wider
            overlaps but cost more (default 200 — comfortably more than the
            ~2.5 s ≈ ~30-50 char overlap we produce on hard-cuts).
        min_overlap_chars: shortest common substring that counts as a real
            overlap.  Below this, treat as coincidence and plain-concat
            (default 20 — long enough to avoid false positives on common
            phrases like "我们" or " the ").

    Returns:
        Single combined transcript.  Empty when *texts* contains no non-empty
        strings.
    """
    cleaned = [t for t in texts if t]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]

    out = cleaned[0]
    for nxt in cleaned[1:]:
        out = _splice_pair(
            out,
            nxt,
            probe_chars=probe_chars,
            min_overlap_chars=min_overlap_chars,
        )
    return out
