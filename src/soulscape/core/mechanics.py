from __future__ import annotations

from enum import Enum
from typing import Any


class Nature(str, Enum):
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
    def get_modifier(nature: "Nature", stat: str) -> float:
        """
        Returns the modifier (0.9, 1.0, 1.1) for a given nature and stat.
        HP is never modified by nature.
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
    def __init__(
        self, name: str, description: str, learned_by: list[Any | None] = []
    ):
        self.name = name
        self.description = description
        self.learned_by = learned_by


class Rarity:
    def __init__(self, name: str, spawn_rate: float):
        self.name = name
        self.spawn_rate = spawn_rate


class Evolution:
    def __init__(
        self,
        evolves_from: Any = None,
        evolves_to: Any = None,
        evolves_at_level: int = None,
    ):
        self.evolves_from = evolves_from
        self.evolves_to = evolves_to
        self.evolves_at_level = evolves_at_level
