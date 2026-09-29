"""Base class for all watchdog checks."""
from abc import ABC, abstractmethod
from typing import Any, Dict


class BaseCheck(ABC):
    """Base class for all monitoring checks."""

    def __init__(self, config: Dict[str, Any], notifier=None):
        self.config = config
        self.notifier = notifier  # shared TelegramNotifier (can be overridden per-check)

    @abstractmethod
    async def run(self) -> None:
        """Execute one check iteration. Send alerts if needed."""
        pass

    @property
    def name(self) -> str:
        return self.__class__.__name__
