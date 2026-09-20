"""Module defining the statistics system for Souls.

This module includes the core Stat enum, the StatSet data structure, and the
SoulStats class for managing base stats, IVs, EVs, and leveling logic.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field
from typing import Any

from shared.enums import Stat
from .mechanics import Nature


@dataclass
class StatSet:
    """Holds a set of 6 core stats + Vision.

    Attributes:
        hp: Hit Points.
        attack: Physical Attack.
        defense: Physical Defense.
        sp_atk: Special Attack.
        sp_def: Special Defense.
        speed: Speed.
        vision: Sensory Range.
    """

    hp: int = 0
    attack: int = 0
    defense: int = 0
    sp_atk: int = 0
    sp_def: int = 0
    speed: int = 0
    vision: int = 0

    def get(self, stat: Stat) -> int:
        """Retrieves the value of a specific stat.

        Args:
            stat: The Stat enum to retrieve.

        Returns:
            The integer value of the stat.
        """
        if stat == Stat.HP:
            return self.hp
        elif stat == Stat.ATTACK:
            return self.attack
        elif stat == Stat.DEFENSE:
            return self.defense
        elif stat == Stat.SP_ATK:
            return self.sp_atk
        elif stat == Stat.SP_DEF:
            return self.sp_def
        elif stat == Stat.SPEED:
            return self.speed
        elif stat == Stat.VISION:
            return self.vision
        return 0

    def to_dict(self) -> dict[str, int]:
        """Serializes the StatSet to a dictionary.

        Returns:
            A dictionary mapping stat attributes to values.
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, int]) -> StatSet:
        """Creates a StatSet from a dictionary.

        Args:
            data: Dictionary of stat values.

        Returns:
            A new StatSet instance.
        """
        return cls(**data) if data else cls()


@dataclass
class SoulStats:
    """Manages the stats of a Soul using the Base/IV/EV system.

    Attributes:
        base: Base stats (species specific).
        ivs: Individual Values (0-31).
        evs: Effort Values (trained).
        nature: Determining nature for stat modifiers.
        level: Current level (1-100).
    """

    base: StatSet
    ivs: StatSet
    evs: StatSet
    nature: Nature
    level: int = 1
    _max_hp_override: int | None = field(default=None, repr=False, compare=False)

    @classmethod
    def create_random(cls, level: int = 5) -> SoulStats:
        """Creates a SoulStats instance with random IVs/Nature and default Base stats.

        Args:
            level: The starting level for the stats.

        Returns:
            A new SoulStats instance.
        """
        # Default Base Stats (Mew-like 100s for now)
        base = StatSet(
            hp=100,
            attack=100,
            defense=100,
            sp_atk=100,
            sp_def=100,
            speed=100,
            vision=100,
        )

        # Random IVs (0-31)
        ivs = StatSet(
            hp=random.randint(0, 31),
            attack=random.randint(0, 31),
            defense=random.randint(0, 31),
            sp_atk=random.randint(0, 31),
            sp_def=random.randint(0, 31),
            speed=random.randint(0, 31),
            vision=random.randint(0, 31),
        )

        # Empty EVs
        evs = StatSet()

        # Random Nature
        nature = random.choice(list(Nature))

        return cls(base=base, ivs=ivs, evs=evs, level=level, nature=nature)

    def calculate_value(self, stat: Stat) -> int:
        """Calculates the final effective value of a stat.

        Uses the Gen 3+ formula:
        HP: ((2 * Base + IV + (EV/4)) * Level / 100) + Level + 10
        Other: (((2 * Base + IV + (EV/4)) * Level / 100) + 5) * Nature

        Args:
            stat: The Stat enum to calculate.

        Returns:
            The calculated integer value of the stat.
        """
        base = self.base.get(stat)
        iv = self.ivs.get(stat)
        ev = self.evs.get(stat)

        # Common inner term
        inner = (2 * base + iv + (ev // 4)) * self.level // 100

        if stat == Stat.HP:
            # HP Formula exception for Shedinja (Base HP 1) is ignored here for generic souls
            if base == 1:
                return 1
            return int(inner + self.level + 10)

        else:
            modifier = Nature.get_modifier(self.nature, stat)
            return int((inner + 5) * modifier)

    @property
    def max_hp(self) -> int:
        """Calculated Maximum HP.

        A Hub-authoritative override wins when set (viewport mode
        applies the Hub stream's max_hp here); otherwise the value
        is derived from base/IV/EV/nature/level as before.
        """
        if self._max_hp_override is not None:
            return self._max_hp_override
        return self.calculate_value(Stat.HP)

    @max_hp.setter
    def max_hp(self, value: int) -> None:
        self._max_hp_override = max(1, int(value))

    @property
    def attack(self) -> int:
        """Calculated Attack stat."""
        return self.calculate_value(Stat.ATTACK)

    @property
    def defense(self) -> int:
        """Calculated Defense stat."""
        return self.calculate_value(Stat.DEFENSE)

    @property
    def sp_atk(self) -> int:
        """Calculated Special Attack stat."""
        return self.calculate_value(Stat.SP_ATK)

    @property
    def sp_def(self) -> int:
        """Calculated Special Defense stat."""
        return self.calculate_value(Stat.SP_DEF)

    @property
    def speed(self) -> int:
        """Calculated Speed stat."""
        return self.calculate_value(Stat.SPEED)

    @property
    def vision(self) -> int:
        """Calculated Vision stat."""
        return self.calculate_value(Stat.VISION)

    def to_dict(self) -> dict[str, Any]:
        """Serializes SoulStats to a dictionary.

        Returns:
            A dictionary containing all stat components.
        """
        return {
            "base": self.base.to_dict(),
            "ivs": self.ivs.to_dict(),
            "evs": self.evs.to_dict(),
            "level": self.level,
            "nature": self.nature.value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SoulStats:
        """Reconstructs SoulStats from a flat dictionary (Hub SQL format)."""
        base = StatSet(
            hp=data.get("stat_hp_base", 100),
            attack=data.get("stat_atk_base", 100),
            defense=data.get("stat_def_base", 100),
            sp_atk=data.get("stat_spa_base", 100),
            sp_def=data.get("stat_spd_base", 100),
            speed=data.get("stat_spe_base", 100),
            vision=data.get("stat_vis_base", 100),
        )
        ivs = StatSet(
            hp=data.get("stat_hp_iv", 0),
            attack=data.get("stat_atk_iv", 0),
            defense=data.get("stat_def_iv", 0),
            sp_atk=data.get("stat_spa_iv", 0),
            sp_def=data.get("stat_spd_iv", 0),
            speed=data.get("stat_spe_iv", 0),
            vision=data.get("stat_vis_iv", 0),
        )
        evs = StatSet(
            hp=data.get("stat_hp_ev", 0),
            attack=data.get("stat_atk_ev", 0),
            defense=data.get("stat_def_ev", 0),
            sp_atk=data.get("stat_spa_ev", 0),
            sp_def=data.get("stat_spd_ev", 0),
            speed=data.get("stat_spe_ev", 0),
            vision=data.get("stat_vis_ev", 0),
        )

        return cls(
            base=base,
            ivs=ivs,
            evs=evs,
            level=data.get("level", 1),
            nature=Nature(data.get("nature", "Hardy")),
        )
