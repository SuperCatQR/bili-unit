# audio — audio pipeline: ASR backends, CDN download, ffmpeg conversion.
#
# Ships:
#   - ASRBackend Protocol + MockASRBackend (testing / interface stability)
#   - MimoASRBackend (real MiMo cloud ASR)
#   - AudioDownloader (bilibili CDN audio stream extraction)
#   - convert_single / convert_m4s_to_mp3 (ffmpeg m4s → mp3 + segmentation)
#   - convert_at_points (VAD-driven per-range trimming)
#   - detect_speech_segments / pick_split_points (Silero VAD via ONNX)
#   - stitch_transcripts (overlap-aware transcript concatenation)
#   - ffmpeg discovery (system + imageio-ffmpeg fallback)

from ._asr_backend import ASRBackend, ASRResult, MockASRBackend, create_asr_backend
from ._asr_cache import ASRCacheStore, CachedSegment
from ._converter import (
    Mp3Segment,
    compute_segment_seconds,
    convert_and_segment,
    convert_at_points,
    convert_m4s_to_mp3,
    convert_single,
)
from ._downloader import AudioDownloader
from ._ffmpeg import FFmpegUnavailable, is_available, resolve_ffmpeg
from ._mimo_backend import MimoASRBackend, create_mimo_backend
from ._stitch import stitch_transcripts
from ._vad import detect_speech_segments, pick_split_points

__all__ = [
    "ASRBackend",
    "ASRCacheStore",
    "ASRResult",
    "AudioDownloader",
    "CachedSegment",
    "FFmpegUnavailable",
    "MimoASRBackend",
    "MockASRBackend",
    "Mp3Segment",
    "compute_segment_seconds",
    "convert_and_segment",
    "convert_at_points",
    "convert_m4s_to_mp3",
    "convert_single",
    "create_asr_backend",
    "create_mimo_backend",
    "detect_speech_segments",
    "is_available",
    "pick_split_points",
    "resolve_ffmpeg",
    "stitch_transcripts",
]
