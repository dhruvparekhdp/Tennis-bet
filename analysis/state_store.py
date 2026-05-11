import asyncio

from analysis.match_state import MatchState


class MatchStateStore:
    """Thread-safe in-memory store for all live match states."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._states: dict[str, MatchState] = {}

    async def update(self, state: MatchState) -> None:
        async with self._lock:
            self._states[state.match_id] = state

    async def get(self, match_id: str) -> MatchState | None:
        async with self._lock:
            return self._states.get(match_id)

    async def get_all(self) -> list[MatchState]:
        async with self._lock:
            return list(self._states.values())

    async def remove(self, match_id: str) -> None:
        async with self._lock:
            self._states.pop(match_id, None)

    async def count(self) -> int:
        async with self._lock:
            return len(self._states)
