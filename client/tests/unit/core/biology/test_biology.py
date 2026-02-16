import pytest

from client.core.biology.biology import SoulBiology
from client.core.biology.gender import Gender
from client.core.biology.species import Species


class TestSoulBiology:
    def test_initialization_defaults(self):
        bio = SoulBiology()
        assert bio.name is not None
        assert bio.species.name == "Soul"
        assert bio.gender is not None
        assert bio.current_health == bio.stats.max_hp
        assert bio.satiety == 100.0
        assert bio.hydration == 100.0

    def test_initialization_custom(self):
        species = Species("Human", [Gender("Male", False)])
        bio = SoulBiology(name="John Doe", species=species)
        assert bio.first_name == "John"
        assert bio.family_name == "Doe"
        assert bio.get_full_name() == "John Doe"
        assert bio.species.name == "Human"

    def test_needs_drain(self):
        bio = SoulBiology()
        initial_satiety = bio.satiety

        # Default drain is 0.2
        bio.decrease_satiety()
        assert bio.satiety == pytest.approx(initial_satiety - 0.2)

        # Active drain is 0.2 * 1.5 = 0.3
        bio.activity_level = "active"
        bio.decrease_satiety()
        assert bio.satiety == pytest.approx(initial_satiety - 0.2 - 0.3)

    def test_health_penalty(self, mock_logger):
        bio = SoulBiology()
        # Set needs to critical
        bio.satiety = 10.0
        bio.hydration = 10.0

        initial_hp = bio.current_health
        bio.check_status()

        # Should have taken damage twice (starvation + dehydration)
        assert bio.current_health < initial_hp
        assert bio.is_alive()

    def test_death(self, mock_logger, mocker):
        bio = SoulBiology()
        bio.current_health = 1
        bio.satiety = 0

        # Mock random to return a value that guarantees death
        mocker.patch("random.randint", return_value=5)

        # Force penalty
        bio.apply_health_penalty()

        assert bio.current_health <= 0
        assert bio.is_dead()
        assert not bio.is_alive()

    def test_serialization(self):
        bio = SoulBiology(name="Jane Doe")
        bio.level = 5
        bio.experience_points = 1000

        data = bio.to_dict()
        assert data["name"] == "Jane Doe"
        assert data["level"] == 5
        assert data["experience_points"] == 1000
        assert "stats" in data

    def test_flat_serialization(self):
        bio = SoulBiology(name="Jane Doe")
        flat = bio.to_flat_dict()
        assert flat["name"] == "Jane Doe"
        assert "stat_hp_base" in flat
        assert "stat_atk_iv" in flat
