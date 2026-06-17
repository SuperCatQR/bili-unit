"""EndpointSpec groups for the fetching catalog."""

from ._channel_upower import channel_and_upower_endpoints
from ._content import content_endpoints
from ._user import user_endpoints
from ._video import video_endpoints

__all__ = [
    "channel_and_upower_endpoints",
    "content_endpoints",
    "user_endpoints",
    "video_endpoints",
]
