from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from soulscape.core.gender import Gender


class Species:
    current_species_id: int = 0

    def __init__(self, name: str, genders: list[Gender]):
        Species.current_species_id += 1
        self.species_id: int = Species.current_species_id
        self.name: str = name
        self.genders: list[Gender] = genders
