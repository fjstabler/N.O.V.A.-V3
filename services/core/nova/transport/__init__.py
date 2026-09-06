"""Local WebSocket bridge between the core service and the desktop shell."""

from .protocol import PROTOCOL_VERSION, Kind, Message, Requests
from .router import RequestRouter
from .server import BridgeService

__all__ = ["PROTOCOL_VERSION", "BridgeService", "Kind", "Message", "RequestRouter", "Requests"]
