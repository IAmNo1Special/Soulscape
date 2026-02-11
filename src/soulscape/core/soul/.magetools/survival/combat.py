from magetools import spell

from soulscape.core.soul import Soul
from soulscape.utils.helpers import action_guard


@action_guard
@spell
def attack(self, target: Soul) -> int | None:
    """Attacks another soul and deals damage based on stats.

    Args:
        target: The Soul instance to attack.

    Returns:
        The amount of damage dealt as an integer, or None if invalid.
    """
    if self.biology.is_dead():
        print("You are dead, you can't attack!")
        return None
    if not target or target.biology.is_dead():
        print("Target is invalid or already dead.")
        return None

    damage = self.biology.stats.attack - target.biology.stats.defense
    if damage < 0:
        damage = 0
    if damage == 0 and self.biology.stats.attack > 0:
        damage = 1

    target.biology.current_health -= damage
    print(
        f"{self.biology.name} attacks {target.biology.name} for {damage} damage!"
    )

    if target.biology.current_health <= 0:
        target.biology.current_health = 0
        print(f"You killed {target.biology.name}!")

    return damage
