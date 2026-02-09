"""Core mechanics definitions for Soulscape."""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .stats import Stat


class Nature(str, Enum):
    """Enumeration of natures affecting stat growth."""

    HARDY = "Hardy"
    LONELY = "Lonely"
    BRAVE = "Brave"
    ADAMANT = "Adamant"
    NAUGHTY = "Naughty"
    BOLD = "Bold"
    DOCILE = "Docile"
    RELAXED = "Relaxed"
    IMPISH = "Impish"
    LAX = "Lax"
    MODEST = "Modest"
    MILD = "Mild"
    BASHFUL = "Bashful"
    RASH = "Rash"
    QUIET = "Quiet"
    CALM = "Calm"
    GENTLE = "Gentle"
    SASSY = "Sassy"
    CAREFUL = "Careful"
    QUIRKY = "Quirky"
    TIMID = "Timid"
    HASTY = "Hasty"
    JOLLY = "Jolly"
    NAIVE = "Naive"
    SERIOUS = "Serious"

    @staticmethod
    def get_modifier(nature: Nature, stat: str | Stat) -> float:
        """Returns the modifier (0.9, 1.0, 1.1) for a given nature and stat.

        HP is never modified by nature.

        Args:
            nature: The Nature to check.
            stat: The name of the stat (e.g., "Attack", "Defense").

        Returns:
            Review the stat modifier (0.9, 1.0, or 1.1).
        """
        # Note: We use string comparison here to avoid circular dependency with Stat enum
        if stat == "HP":
            return 1.0

        # Define modifiers: (Increased Stat, Decreased Stat)
        modifiers = {
            Nature.HARDY: (None, None),
            Nature.LONELY: ("Attack", "Defense"),
            Nature.BRAVE: ("Attack", "Speed"),
            Nature.ADAMANT: ("Attack", "Sp. Atk"),
            Nature.NAUGHTY: ("Attack", "Sp. Def"),
            Nature.BOLD: ("Defense", "Attack"),
            Nature.DOCILE: (None, None),
            Nature.RELAXED: ("Defense", "Speed"),
            Nature.IMPISH: ("Defense", "Sp. Atk"),
            Nature.LAX: ("Defense", "Sp. Def"),
            Nature.MODEST: ("Sp. Atk", "Attack"),
            Nature.MILD: ("Sp. Atk", "Defense"),
            Nature.BASHFUL: (None, None),
            Nature.RASH: ("Sp. Atk", "Sp. Def"),
            Nature.QUIET: ("Sp. Atk", "Speed"),
            Nature.CALM: ("Sp. Def", "Attack"),
            Nature.GENTLE: ("Sp. Def", "Defense"),
            Nature.SASSY: ("Sp. Def", "Speed"),
            Nature.CAREFUL: ("Sp. Def", "Sp. Atk"),
            Nature.QUIRKY: (None, None),
            Nature.TIMID: ("Speed", "Attack"),
            Nature.HASTY: ("Speed", "Defense"),
            Nature.JOLLY: ("Speed", "Sp. Atk"),
            Nature.NAIVE: ("Speed", "Sp. Def"),
            Nature.SERIOUS: (None, None),
        }

        increased, decreased = modifiers.get(nature, (None, None))
        if stat == increased:
            return 1.1
        if stat == decreased:
            return 0.9
        return 1.0


class Ability:
    """Represents a special ability or skill."""

    def __init__(
        self,
        name: str,
        description: str,
        learned_by: list[Any | None] | None = None,
    ):
        """Initializes an Ability.

        Args:
            name: Name of the ability.
            description: Description of what it does.
            learned_by: List of species that can learn this. Defaults to None.
        """
        self.name: str = name
        self.description: str = description
        self.learned_by: list[Any | None] = learned_by or []


class Rarity:
    """Defines the rarity and spawn rate of an entity."""

    def __init__(self, name: str, spawn_rate: float):
        """Initializes Rarity.

        Args:
            name: Name of the rarity tier (e.g., "Common").
            spawn_rate: Probability of spawning (0.0-1.0).
        """
        self.name: str = name
        self.spawn_rate: float = spawn_rate


class Evolution:
    """Defines evolutionary paths.

    Attributes:
        evolves_from: The base form.
        evolves_to: The evolved form.
        evolves_at_level: Level requirement for evolution.
    """

    def __init__(
        self,
        evolves_from: Any = None,
        evolves_to: Any = None,
        evolves_at_level: int | None = None,
    ) -> None:
        """Initializes an Evolution definition.

        Args:
            evolves_from: The base form.
            evolves_to: The evolved form.
            evolves_at_level: Level requirement for evolution.
        """
        self.evolves_from: Any = evolves_from
        self.evolves_to: Any = evolves_to
        self.evolves_at_level: int | None = evolves_at_level
