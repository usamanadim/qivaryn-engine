from .client import InstagramClient
from .controller import DownloadController, DownloadStats
from .database import StateDatabase
from .downloader import QivarynEngine
from .platforms import FacebookClient, TikTokClient, YouTubeClient

InstagramEngine = QivarynEngine

__version__ = "0.2.5"
__all__ = [
    "QivarynEngine",
    "InstagramEngine",
    "InstagramClient",
    "TikTokClient",
    "YouTubeClient",
    "FacebookClient",
    "StateDatabase",
    "DownloadController",
    "DownloadStats",
]
