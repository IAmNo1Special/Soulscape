from __future__ import annotations

import random
from datetime import datetime
from typing import TYPE_CHECKING, Any

from soulscape.system.logger import log

from .gender import Gender, all_genders
from .species import Species
from .stats import SoulStats

if TYPE_CHECKING:
    from ..soul.soul import Soul


class SoulBiology:
    _current_soul_id: int = 0

    # Known Names
    KNOWN_MALE_FIRST_NAMES: list[str] = [
        "Jackson",
        "John",
        "Jack",
        "Malcom",
        "Fatin",
    ]
    KNOWN_FEMALE_FIRST_NAMES: list[str] = [
        "Jane",
        "Lily",
        "Mallory",
        "Fatima",
        "Fatinah",
    ]

    def __init__(
        self,
        species: Species | None = None,
        name: str | None = None,
        birth_mother: Soul | None = None,
        birth_father: Soul | None = None,
        gender: Gender | None = None,
        stats: SoulStats | None = None,
        current_location: tuple[float, float] = (0.0, 0.0),
    ):
        SoulBiology._current_soul_id += 1
        self.soul_id: int = SoulBiology._current_soul_id
        self.name = name
        self.species = species
        self.birth_mother = birth_mother
        self.birth_father = birth_father
        self.gender = gender
        self.stats = stats
        self.current_location = current_location

        # Species & Gender
        # We need a default species if none provided, to avoid crashes in simulation
        # In a real app, maybe we'd require it, but for compatibility:
        if species is None:
            # Basic default if not provided (e.g. legacy/testing)
            # Ideally simulation should provide this.
            # We create a fallback locally if needed or assume user handles it.
            # For now, let's allow None but simulation methods might need checking.
            self.species = Species(
                "Soul", [Gender("Male", False), Gender("Female", True)]
            )
        else:
            self.species = species

        if gender is None:
            self.gender: Gender = random.choice(all_genders)
        else:
            self.gender: Gender = gender

        # Name Handling
        self.first_name: str | None = None
        self.family_name: str | None = "Doe"

        if name:
            parts = name.split(" ", 1)
            self.first_name = parts[0]
            if len(parts) > 1:
                self.family_name = parts[1]

        self.name = self.get_full_name()  # Update interaction name

        # Lineage
        self.birth_mother: Soul | None = birth_mother
        self.birth_father: Soul | None = birth_father
        self.birth_datetime: datetime = datetime.now()

        self.current_location: tuple[float, float] = current_location
        self.hometown: tuple[float, float] = current_location

        # Stats & Biology
        if stats:
            self.stats = stats
        else:
            self.stats = SoulStats.create_random()

        self.experience_points: float = 0.0
        self.level: int = 1

        # Derived Stats
        self.current_health: int = self.stats.max_hp

        # Needs (Values drain every 1.0s update interval)
        self.satiety: float = 100.0
        self.hydration: float = 100.0
        self.satiety_drain_rate: float = (
            0.2  # Takes ~8.3 mins to drain from 100 to 0 (1 point every 5s)
        )
        self.hydration_drain_rate: float = (
            0.2  # Takes ~8.3 mins to drain from 100 to 0 (1 point every 5s)
        )
        self.activity_level: str = (
            "resting"  # Can be 'resting', 'active', 'fighting'.
        )
        # Sensory System
        self.sensations: list[str] = []

    def get_full_name(self) -> str:
        """Assembles the first and family names into a readable format.

        Returns:
            The combined full name, or a generic placeholder if not named.
        """
        if self.first_name and self.family_name:
            return f"{self.first_name} {self.family_name}"
        elif self.first_name:
            return self.first_name
        return f"Soul #{self.soul_id}"

    def get_id(self) -> int:
        """Returns the unique numeric ID of the soul."""
        return self.soul_id

    def get_species(self) -> Species:
        """Returns the species object associated with this soul."""
        return self.species

    def get_gender(self) -> str:
        """Returns the string name of the soul's gender."""
        return self.gender.gender_name if self.gender else "Unknown"

    def get_current_health(self) -> int:
        """Returns the current HP level."""
        return self.current_health

    @property
    def max_health(self) -> int:
        """Returns the maximum HP level from biological stats."""
        return self.stats.max_hp

    def get_max_health(self) -> int:
        """Returns the maximum HP level from biological stats."""
        return self.max_health

    def get_birth_datetime(self) -> datetime:
        """Returns the timestamp of when this soul was created."""
        return self.birth_datetime

    def get_hometown(self) -> str:
        """Returns the name of the place where the soul was born."""
        return self.hometown

    def get_age(self) -> int:
        """Calculates time elapsed since birth in seconds."""
        if hasattr(self, "birth_datetime"):
            current_datetime: datetime = datetime.now()
            time_since_birth = current_datetime - self.birth_datetime
            return int(time_since_birth.total_seconds())
        return 0

    def is_alive(self) -> bool:
        """Checks if the soul's vitality is above zero."""
        return self.current_health > 0

    def is_dead(self) -> bool:
        """Checks if the soul's vitality has reached zero."""
        return self.current_health <= 0

    def decrease_satiety(self) -> None:
        """Naturally drains satiety over time based on current activity.

        This simulates biological energy consumption. More active souls burn
        energy faster.
        """
        rate = self.satiety_drain_rate * self.get_activity_multiplier()
        self.satiety = round(self.satiety - rate, 2)
        if self.satiety < 0:
            self.satiety = 0

    def decrease_hydration(self) -> None:
        """Naturally drains hydration over time.

        Simulates the constant need for fluids.
        """
        rate = self.hydration_drain_rate  # No environment factor
        self.hydration = round(self.hydration - rate, 2)
        if self.hydration < 0:
            self.hydration = 0

    def get_activity_multiplier(self) -> float:
        """Returns the energy consumption scale for current behavior."""
        if self.activity_level == "active":
            return 1.5
        elif self.activity_level == "fighting":
            return 2.0
        return 1.0

    def check_status(self) -> None:
        """Evaluates biological needs and applies starvation/dehydration damage.

        If needs are critically low, the soul's health will begin to wither.
        """
        if self.is_dead():
            return

        if self.satiety < 20:
            log.debug(f"{self.name} is starving!")
            self.apply_health_penalty()
        elif self.satiety < 50:
            log.debug(f"{self.name} is hungry.")

        if self.hydration < 20:
            log.debug(f"{self.name} is dehydrated!")
            self.apply_health_penalty()
        elif self.hydration < 50:
            log.debug(f"{self.name} is thirsty.")

    def apply_health_penalty(self) -> None:
        """Reduces current HP as a penalty for neglected biological needs."""
        if self.current_health > 0:
            health_penalty = random.randint(1, 5)
            self.current_health -= health_penalty
            log.debug(
                f"{self.name} suffers a health penalty of {health_penalty}!"
            )
            if self.current_health <= 0:
                self.current_health = 0
                log.info(f"--- {self.name} HAS PERISHED ---")
        else:
            # Soul is already dead, no further penalties
            pass

    def to_dict(self) -> dict[str, Any]:
        """Serializes biology state to a dictionary."""
        return {
            "name": self.name,
            "first_name": self.first_name,
            "family_name": self.family_name,
            "species": self.species.name if self.species else "Human",
            "gender": self.gender.to_dict() if self.gender else None,
            "stats": self.stats.to_dict() if self.stats else None,
            "satiety": round(self.satiety, 2),
            "hydration": round(self.hydration, 2),
            "experience_points": self.experience_points,
            "level": self.level,
            "current_health": self.current_health,
            "current_location": self.current_location,
            "hometown": self.hometown,
            "birth_datetime": self.birth_datetime.isoformat(),
            "activity_level": self.activity_level,
            "birth_mother_id": (
                self.birth_mother.biology.soul_id if self.birth_mother else None
            ),
            "birth_father_id": (
                self.birth_father.biology.soul_id if self.birth_father else None
            ),
        }

    def to_flat_dict(self) -> dict[str, Any]:
        """Serializes biology to a fully flat dictionary format."""
        data = {
            "name": self.name,
            "first_name": self.first_name,
            "family_name": self.family_name,
            "species": self.species.name if self.species else None,
            "gender": self.gender.gender_name if self.gender else None,
            "level": self.level,
            "hp": self.current_health,
            "max_health": self.stats.max_hp,
            "satiety": self.satiety,
            "hydration": self.hydration,
            "xp": int(self.experience_points),
            "position": [self.current_location[0], self.current_location[1]],
            "hometown": [self.hometown[0], self.hometown[1]],
            "birth_date": (
                self.birth_datetime.isoformat() if self.birth_datetime else None
            ),
            "activity": self.activity_level,
            "mother_id": (
                self.birth_mother.biology.soul_id if self.birth_mother else None
            ),
            "father_id": (
                self.birth_father.biology.soul_id if self.birth_father else None
            ),
        }

        # Flatten Stats
        if self.stats:
            stats_dict = self.stats.to_dict()
            base = stats_dict.get("base", {})
            ivs = stats_dict.get("ivs", {})
            evs = stats_dict.get("evs", {})

            data.update(
                {
                    "stat_hp_base": base.get("hp"),
                    "stat_atk_base": base.get("attack"),
                    "stat_def_base": base.get("defense"),
                    "stat_spa_base": base.get("sp_atk"),
                    "stat_spd_base": base.get("sp_def"),
                    "stat_spe_base": base.get("speed"),
                    "stat_vis_base": base.get("vision"),
                    "stat_hp_iv": ivs.get("hp"),
                    "stat_atk_iv": ivs.get("attack"),
                    "stat_def_iv": ivs.get("defense"),
                    "stat_spa_iv": ivs.get("sp_atk"),
                    "stat_spd_iv": ivs.get("sp_def"),
                    "stat_spe_iv": ivs.get("speed"),
                    "stat_vis_iv": ivs.get("vision"),
                    "stat_hp_ev": evs.get("hp"),
                    "stat_atk_ev": evs.get("attack"),
                    "stat_def_ev": evs.get("defense"),
                    "stat_spa_ev": evs.get("sp_atk"),
                    "stat_spd_ev": evs.get("sp_def"),
                    "stat_spe_ev": evs.get("speed"),
                    "stat_vis_ev": evs.get("vision"),
                    "nature": stats_dict.get("nature"),
                }
            )
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SoulBiology:
        """Reconstructs biology from a dictionary (assumes flat protocol)."""
        name = data.get("name")
        first_name = data.get("first_name")
        family_name = data.get("family_name")

        # Location handling (ensure it's a tuple)
        loc = data.get("position")
        if loc is None:
            # Fallback to current_location if position is missing (though it shouldn't be)
            loc = data.get("current_location", (0.0, 0.0))

        if isinstance(loc, list):
            loc = tuple(loc)

        bio = cls(
            name=name,
            current_location=loc,
        )
        bio.first_name = first_name
        bio.family_name = family_name
        bio.satiety = data.get("satiety", 100.0)
        bio.hydration = data.get("hydration", 100.0)
        bio.experience_points = data.get("xp", 0.0)
        bio.level = data.get("level", 1)
        bio.current_health = data.get("hp", 100)

        home = data.get("hometown", loc)
        if isinstance(home, list):
            home = tuple(home)
        bio.hometown = home
        bio.activity_level = data.get("activity", "resting")

        birth_date = data.get("birth_date")
        if birth_date:
            try:
                bio.birth_datetime = datetime.fromisoformat(birth_date)
            except (ValueError, TypeError):
                pass

        # Stats are always flat now
        bio.stats = SoulStats.from_dict(data)

        # Handle flat gender
        gender_val = data.get("gender")
        # Try to match with existing gender objects
        gender_str = str(gender_val).lower() if gender_val else "male"
        matched_gender = next(
            (g for g in all_genders if g.gender_name.lower() == gender_str),
            all_genders[0],  # Default to Male
        )
        bio.gender = matched_gender

        return bio
