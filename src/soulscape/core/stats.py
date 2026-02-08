from __future__ import annotations

import random
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Dict

from soulscape.core.mechanics import Nature


class Stat(str, Enum):
    HP = "HP"
    ATTACK = "Attack"
    DEFENSE = "Defense"
    SP_ATK = "Sp. Atk"
    SP_DEF = "Sp. Def"
    SPEED = "Speed"
    VISION = "Vision"


@dataclass
class StatSet:
    """Holds a set of 6 core stats."""

    hp: int = 0
    attack: int = 0
    defense: int = 0
    sp_atk: int = 0
    sp_def: int = 0
    speed: int = 0
    vision: int = 0

    def get(self, stat: Stat) -> int:
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

    def to_dict(self) -> Dict[str, int]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, int]) -> "StatSet":
        return cls(**data) if data else cls()


@dataclass
class SoulStats:
    """
    Manages the stats of a Soul using the Base/IV/EV system.
    """

    base: StatSet
    ivs: StatSet
    evs: StatSet
    nature: Nature
    level: int = 1

    @classmethod
    def create_random(cls, level: int = 5) -> "SoulStats":
        """Creates a SoulStats instance with random IVs and Nature, and default Base stats."""
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
        """
        Calculates the final effective value of a stat using the Gen 3+ formula.
        HP: ((2 * Base + IV + (EV/4)) * Level / 100) + Level + 10
        Other: (((2 * Base + IV + (EV/4)) * Level / 100) + 5) * Nature
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
        return self.calculate_value(Stat.HP)

    @property
    def attack(self) -> int:
        return self.calculate_value(Stat.ATTACK)

    @property
    def defense(self) -> int:
        return self.calculate_value(Stat.DEFENSE)

    @property
    def sp_atk(self) -> int:
        return self.calculate_value(Stat.SP_ATK)

    @property
    def sp_def(self) -> int:
        return self.calculate_value(Stat.SP_DEF)

    @property
    def speed(self) -> int:
        return self.calculate_value(Stat.SPEED)

    @property
    def vision(self) -> int:
        return self.calculate_value(Stat.VISION)

    def to_dict(self) -> Dict:
        return {
            "base": self.base.to_dict(),
            "ivs": self.ivs.to_dict(),
            "evs": self.evs.to_dict(),
            "level": self.level,
            "nature": self.nature.value,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "SoulStats":
        return cls(
            base=StatSet.from_dict(data.get("base", {})),
            ivs=StatSet.from_dict(data.get("ivs", {})),
            evs=StatSet.from_dict(data.get("evs", {})),
            level=data.get("level", 1),
            nature=Nature(data.get("nature", "Hardy")),
        )
