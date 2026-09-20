from enum import Enum


class Stat(str, Enum):
    """Enumeration of core stats."""

    HP = "HP"
    ATTACK = "Attack"
    DEFENSE = "Defense"
    SP_ATK = "Sp. Atk"
    SP_DEF = "Sp. Def"
    SPEED = "Speed"
    VISION = "Vision"
