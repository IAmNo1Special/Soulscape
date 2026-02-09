"""Species definition for Souls."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .gender import Gender


class Species:
    """Represents a species of Soul."""

    current_species_id: int = 0

    def __init__(self, name: str, genders: list[Gender]) -> None:
        """Initializes a Species.

        Args:
            name: Name of the species (e.g., "Human").
            genders: List of possible genders for this species.
        """
        Species.current_species_id += 1
        self.species_id: int = Species.current_species_id
        self.name: str = name
        self.genders: list[Gender] = genders

    def to_dict(self) -> dict[str, Any]:
        """Serializes species to a dictionary."""
        return {
            "name": self.name,
            "genders": [g.to_dict() for g in self.genders],
        }

    def __repr__(self) -> str:
        """String representation."""
        return self.name
