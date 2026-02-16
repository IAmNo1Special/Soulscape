import random

from magetools import spell

from client.core.biology import SoulBiology
from client.core.soul import Soul


@spell
def name_child(self, child: Soul) -> None:
    """Names a child soul based on gender and known names.

    Args:
        child: The child Soul instance to be named.
    """
    if not child.biology.first_name:
        if child.biology.gender.gender_name == "male":
            names = SoulBiology.KNOWN_MALE_FIRST_NAMES
        else:
            names = SoulBiology.KNOWN_FEMALE_FIRST_NAMES  # simplified check

        if names:
            child.biology.first_name = random.choice(names)
        else:
            child.biology.first_name = "Unnamed"

    if not child.biology.family_name:
        child.biology.family_name = self.biology.family_name

    child.biology.name = child.biology.get_full_name()  # Update display name

    gender_str = "boy" if child.biology.gender.gender_name == "male" else "girl"
    pronoun = "him" if child.biology.gender.gender_name == "male" else "her"
    print(
        f"{self.biology.name} had a baby {gender_str} and named {pronoun} {child.biology.name}"
    )


@spell
def claim_child(self, child: Soul) -> None:
    """Sets the parent-child relationship based on this soul's gender.

    Args:
        child: The child Soul instance.
    """
    if self.biology.gender.gender_name == "male":
        child.biology.birth_father = self
    elif self.biology.gender.gender_name == "female":
        child.biology.birth_mother = self


@spell
def give_birth(self) -> Soul | None:
    """Simulates giving birth to a new soul if gender allows.

    Returns:
        A new Soul instance if successful, None otherwise.
    """
    if self.biology.gender.can_give_birth:
        child = Soul(
            species=self.biology.species,
            gender=None,
            birth_mother=self,
            current_location=self.biology.current_location,
            # Pass None for visual args to default
        )
        print(f"{self.biology.name} gave birth.")
        return child
    else:
        print(f"{self.biology.gender}s can't give birth.")
        return None
