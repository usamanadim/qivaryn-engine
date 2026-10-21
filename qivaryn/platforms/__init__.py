"""Platform registry.

Adding a platform means implementing :class:`~qivaryn.platforms.base.BasePlatformClient`
and registering it here; the CLI, the engine and the state database pick it up
automatically.

The Instagram client is imported lazily because :mod:`qivaryn.client` imports
this package for the shared platform contract.  Keeping that import deferred
avoids a circular import at start-up.
"""

from typing import Dict, Tuple, Type

from .base import AccountFeed, BasePlatformClient, make_child, make_item
from .facebook import FacebookClient
from .tiktok import TikTokClient
from .youtube import YouTubeClient

#: Ordered so that the most commonly used platforms appear first in ``--help``.
PLATFORM_NAMES: Tuple[str, ...] = ("instagram", "tiktok", "youtube", "facebook")

_CLIENTS: Dict[str, Type[BasePlatformClient]] = {
    "tiktok": TikTokClient,
    "youtube": YouTubeClient,
    "facebook": FacebookClient,
}


def get_client_class(platform: str) -> Type[BasePlatformClient]:
    """Returns the client class for ``platform``, or raises ``KeyError``."""
    key = str(platform).lower()
    if key == "instagram":
        from ..client import InstagramClient

        return InstagramClient
    try:
        return _CLIENTS[key]
    except KeyError as exc:
        supported = ", ".join(PLATFORM_NAMES)
        raise KeyError(
            f"Unsupported platform '{platform}'. Available platforms: {supported}."
        ) from exc


def supported_media_types(platform: str) -> Tuple[str, ...]:
    """Returns the ``--type`` values accepted by ``platform``."""
    return get_client_class(platform).supported_media_types


#: Union of every platform's ``--type`` values, used for CLI validation.
ALL_MEDIA_TYPES = (
    "all",
    "posts",
    "videos",
    "reels",
    "carousels",
    "shorts",
    "streams",
    "photos",
)

__all__ = [
    "AccountFeed",
    "ALL_MEDIA_TYPES",
    "BasePlatformClient",
    "FacebookClient",
    "PLATFORM_NAMES",
    "TikTokClient",
    "YouTubeClient",
    "get_client_class",
    "make_child",
    "make_item",
    "supported_media_types",
]
