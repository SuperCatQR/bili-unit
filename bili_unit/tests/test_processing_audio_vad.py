# tests for VAD-aware audio segmentation: pick_split_points + stitch_transcripts.
#
# Both modules are tested without touching ONNX or ffmpeg — pick_split_points
# is pure logic and stitch_transcripts uses only difflib.

from bili_unit.processing.audio import pick_split_points, stitch_transcripts

# ---------- pick_split_points ----------------------------------------------


def test_pick_split_points_short_clip_returns_single_span():
    """Clip already under max_seg → single (0, duration) span, no cuts."""
    plan = pick_split_points(
        duration_seconds=300.0,
        speech_segments=[(10.0, 290.0)],
        max_seg=830.0,
    )
    assert plan == [(0.0, 300.0)]


def test_pick_split_points_zero_duration_returns_empty():
    assert pick_split_points(0.0, [], max_seg=830.0) == []


def test_pick_split_points_cuts_at_silence_midpoint():
    """A clear silence gap inside the search window is preferred to a hard cut."""
    # Duration 1000 s; one big silence gap from 700-720 s.  Window for first
    # cut is [60, 830]; gap midpoint 710 is inside, so that's the chosen cut.
    plan = pick_split_points(
        duration_seconds=1000.0,
        speech_segments=[(0.0, 700.0), (720.0, 1000.0)],
        max_seg=830.0,
        min_seg=60.0,
        overlap_sec=2.5,
    )
    assert len(plan) == 2
    assert plan[0] == (0.0, 710.0)
    assert plan[1] == (710.0, 1000.0)


def test_pick_split_points_prefers_latest_qualifying_gap():
    """When several gaps are inside the window, pick the latest — fewer total segments."""
    # Gaps at 200-205 (mid 202.5) and 800-810 (mid 805); both inside [60, 830].
    plan = pick_split_points(
        duration_seconds=1500.0,
        speech_segments=[
            (0.0, 200.0),
            (205.0, 800.0),
            (810.0, 1500.0),
        ],
        max_seg=830.0,
    )
    # First cut should be 805 (latest qualifying gap), not 202.5.
    assert plan[0] == (0.0, 805.0)


def test_pick_split_points_continuous_speech_falls_back_to_overlap_hard_cut():
    """No silence gaps inside any window → hard-cut at max_seg with overlap."""
    # Single huge speech region covering the whole clip — no gaps at all.
    plan = pick_split_points(
        duration_seconds=2000.0,
        speech_segments=[(0.0, 2000.0)],
        max_seg=830.0,
        min_seg=60.0,
        overlap_sec=2.5,
    )
    # First cut at 830, next start at 830 - 2.5 = 827.5 (overlap).
    assert plan[0] == (0.0, 830.0)
    assert plan[1][0] == 827.5
    # All adjacent pairs should overlap by exactly 2.5 s for the hard-cut path.
    for prev, nxt in zip(plan, plan[1:], strict=False):
        assert prev[1] - nxt[0] == 2.5
    # Last segment must reach the end of the clip.
    assert plan[-1][1] == 2000.0


def test_pick_split_points_skips_gaps_before_min_seg():
    """A gap earlier than min_seg must be ignored — too-short first segment."""
    # Gap 5-10 s (mid 7.5) is well before min_seg=60 → ignored.  The next
    # qualifying gap is at 750-760 (mid 755).
    plan = pick_split_points(
        duration_seconds=1500.0,
        speech_segments=[
            (0.0, 5.0),
            (10.0, 750.0),
            (760.0, 1500.0),
        ],
        max_seg=830.0,
        min_seg=60.0,
    )
    assert plan[0] == (0.0, 755.0)


def test_pick_split_points_pathological_min_geq_max_does_not_loop():
    """min_seg >= max_seg should not infinite-loop or crash."""
    plan = pick_split_points(
        duration_seconds=2000.0,
        speech_segments=[(0.0, 2000.0)],
        max_seg=500.0,
        min_seg=600.0,  # invalid: >= max_seg
    )
    # Should still produce a finite plan covering the whole clip.
    assert plan[-1][1] == 2000.0
    assert len(plan) >= 2


def test_pick_split_points_clamps_out_of_range_speech():
    """Speech segments extending past duration are clamped, not crashed."""
    plan = pick_split_points(
        duration_seconds=1000.0,
        speech_segments=[(-50.0, 700.0), (720.0, 5000.0)],
        max_seg=830.0,
    )
    assert plan[-1][1] == 1000.0
    assert plan[0][1] == 710.0  # midpoint of gap 700-720 still found


# ---------- stitch_transcripts ---------------------------------------------


def test_stitch_empty_returns_empty():
    assert stitch_transcripts([]) == ""
    assert stitch_transcripts(["", "", None]) == ""  # type: ignore[list-item]


def test_stitch_single_returns_as_is():
    assert stitch_transcripts(["hello world"]) == "hello world"


def test_stitch_no_overlap_concatenates_with_space():
    # Short, distinct strings → no common substring of >=20 chars → plain join.
    out = stitch_transcripts(["abc", "xyz"])
    assert out == "abc xyz"


def test_stitch_detects_and_dedups_chinese_overlap():
    # 25-char overlap at the seam, well above the 20-char threshold.
    overlap = "今天我们来聊一下大模型的上下文管理与历史记录策略"  # 24 chars
    overlap += "了"  # 25 chars total
    left = "段落一开头一些文字" + overlap
    right = overlap + "段落二的剩余正文，与段落一无关。"

    out = stitch_transcripts([left, right])
    # Overlap should appear exactly once in the output.
    assert out.count(overlap) == 1
    # Both unique parts must survive.
    assert "段落一开头一些文字" in out
    assert "段落二的剩余正文" in out


def test_stitch_short_overlap_falls_through_to_plain_join():
    """An overlap below min_overlap_chars is treated as coincidence, not joined on."""
    # Both pieces share "我们" (2 chars) — below the 20-char threshold.
    left = "前一段我们"
    right = "我们后一段"
    out = stitch_transcripts([left, right])
    # Plain space join — both halves retained verbatim, no overlap dedup.
    assert out == "前一段我们 我们后一段"


def test_stitch_three_segments_dedups_each_seam_independently():
    seam_a = "A" * 30  # well past threshold
    seam_b = "B" * 30
    s1 = "head_one_" + seam_a
    s2 = seam_a + "middle_two_" + seam_b
    s3 = seam_b + "tail_three"

    out = stitch_transcripts([s1, s2, s3])
    assert out.count(seam_a) == 1
    assert out.count(seam_b) == 1
    assert "head_one_" in out
    assert "middle_two_" in out
    assert "tail_three" in out


def test_stitch_respects_custom_probe_window():
    """Probe window of 5 chars cannot see a 25-char overlap deeper in the strings."""
    overlap = "X" * 25
    left = "padding_left_padding_left_padding_left" + overlap
    right = overlap + "padding_right_padding_right"

    # Default probe (200) should see the overlap and dedup.
    default_out = stitch_transcripts([left, right])
    assert default_out.count(overlap) == 1

    # Tiny probe (5) cannot see the overlap → plain join, overlap appears twice.
    narrow_out = stitch_transcripts(
        [left, right],
        probe_chars=5,
        min_overlap_chars=20,
    )
    assert narrow_out.count(overlap) == 2
