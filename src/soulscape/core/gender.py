"""Gender definition for Souls."""

from __future__ import annotations

from typing import Any


class Gender:
    """Represents a biological gender for a species."""

    current_gender_id: int = 0

    def __init__(self, gender_name: str, can_give_birth: bool) -> None:
        """Initializes a Gender.

        Args:
            gender_name: Name of the gender (e.g., "Male", "Female").
            can_give_birth: Whether this gender can produce offspring.
        """
        Gender.current_gender_id += 1
        self.gender_id: int = Gender.current_gender_id
        self.gender_name: str = gender_name
        self.can_give_birth: bool = can_give_birth

    def __repr__(self) -> str:
        """String representation."""
        return self.gender_name

    def __eq__(self, other: Any) -> bool:
        """Equality check."""
        if isinstance(other, str):
            return self.gender_name == other
        return super().__eq__(other)
