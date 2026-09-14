import queue
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Command(ABC):
    """Base class for all game commands."""

    priority: int = 10
    timestamp: float = field(default_factory=time.time)

    def __lt__(self, other: "Command") -> bool:
        if not isinstance(other, Command):
            raise NotImplementedError("Cannot compare Command with non-Command")
        # Lower number = higher priority for PriorityQueue
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.timestamp < other.timestamp

    @abstractmethod
    def execute(self, soul: Any) -> None:
        """Executes the command on the given context (Soul)."""
        pass


class CommandQueue:
    """Thread-safe priority queue for game commands."""

    def __init__(self):
        self._queue: queue.PriorityQueue = queue.PriorityQueue()

    def put(self, command: Command) -> None:
        """Enqueues a command."""
        self._queue.put(command)

    def get(self, block: bool = False) -> Optional[Command]:
        """Dequeues a command. Returns None if empty and not blocking."""
        try:
            return self._queue.get(block=block)
        except queue.Empty:
            return None

    def empty(self) -> bool:
        """Checks if the queue is empty."""
        return self._queue.empty()

    def qsize(self) -> int:
        """Returns the approximate size of the queue."""
        return self._queue.qsize()
