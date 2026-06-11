# audio — audio pipeline: ASR backends, CDN download, ffmpeg conversion.
#
# Ships:
#   - ASRBackend Protocol + MockASRBackend (testing / interface stability)
#   - MimoASRBackend (real MiMo cloud ASR)
#   - AudioDownloader (bilibili CDN audio stream extraction)
#   - convert_single / convert_m4s_to_mp3 (ffmpeg m4s → mp3 + segmentation)
#   - ffmpeg discovery (system + imageio-ffmpeg fallback)

from ._asr_backend import ASRBackend, ASRResult, MockASRBackend, create_asr_backend
from ._converter import (
    compute_segment_seconds,
    convert_and_segment,
    convert_m4s_to_mp3,
    convert_single,
)
from ._downloader import AudioDownloader
from ._ffmpeg import FFmpegUnavailable, is_available, resolve_ffmpeg
from ._mimo_backend import MimoASRBackend, create_mimo_backend

__all__ = [
    "ASRBackend",
    "ASRResult",
    "AudioDownloader",
    "FFmpegUnavailable",
    "MimoASRBackend",
    "MockASRBackend",
    "compute_segment_seconds",
    "convert_and_segment",
    "convert_m4s_to_mp3",
    "convert_single",
    "create_asr_backend",
    "create_mimo_backend",
    "is_available",
    "resolve_ffmpeg",
]
