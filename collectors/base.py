from abc import ABC, abstractmethod


class BaseCollector(ABC):
    @abstractmethod
    async def fetch(self) -> None:
        """Fetch data and update the shared MatchStateStore."""
