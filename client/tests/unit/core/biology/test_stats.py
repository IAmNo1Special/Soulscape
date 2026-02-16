import pytest

from client.core.biology.mechanics import Nature
from client.core.biology.stats import SoulStats, Stat, StatSet


# Fixture for basic stats
@pytest.fixture
def basic_stat_set():
    return StatSet(
        hp=100,
        attack=100,
        defense=100,
        sp_atk=100,
        sp_def=100,
        speed=100,
        vision=100,
    )


@pytest.fixture
def zero_stat_set():
    return StatSet()


class TestStatSet:
    def test_initialization(self, basic_stat_set):
        assert basic_stat_set.hp == 100
        assert basic_stat_set.vision == 100

    def test_get_stat(self, basic_stat_set):
        assert basic_stat_set.get(Stat.HP) == 100
        assert basic_stat_set.get(Stat.ATTACK) == 100
        assert basic_stat_set.get(Stat.VISION) == 100
        assert basic_stat_set.get("non_existent") == 0

    def test_to_dict(self, basic_stat_set):
        data = basic_stat_set.to_dict()
        assert data["hp"] == 100
        assert data["vision"] == 100

    def test_from_dict(self):
        data = {"hp": 50, "attack": 60, "vision": 70}
        stats = StatSet.from_dict(data)
        assert stats.hp == 50
        assert stats.attack == 60
        assert stats.vision == 70  # Check custom stat
        assert stats.defense == 0  # Default value


class TestSoulStats:
    def test_create_random(self):
        stats = SoulStats.create_random(level=5)
        assert stats.level == 5
        assert isinstance(stats.nature, Nature)
        # Check IVs range
        assert 0 <= stats.ivs.hp <= 31
        assert 0 <= stats.ivs.attack <= 31

    def test_calculate_value_hp(self, basic_stat_set, zero_stat_set):
        # Formula: ((2 * Base + IV + (EV/4)) * Level / 100) + Level + 10
        # Case: Base=100, IV=0, EV=0, Level=100
        # ((200 + 0 + 0) * 1) + 100 + 10 = 310
        stats = SoulStats(
            base=basic_stat_set,
            ivs=zero_stat_set,
            evs=zero_stat_set,
            level=100,
            nature=Nature.HARDY,  # Neutral
        )
        assert stats.max_hp == 310
        assert stats.calculate_value(Stat.HP) == 310

    def test_calculate_value_stat_neutral_nature(
        self, basic_stat_set, zero_stat_set
    ):
        # Formula: (((2 * Base + IV + (EV/4)) * Level / 100) + 5) * Nature
        # Case: Base=100, IV=0, EV=0, Level=100, Neutral Nature (1.0)
        # ((200) * 1) + 5 = 205
        stats = SoulStats(
            base=basic_stat_set,
            ivs=zero_stat_set,
            evs=zero_stat_set,
            level=100,
            nature=Nature.HARDY,
        )
        assert stats.attack == 205

    def test_calculate_value_stat_beneficial_nature(
        self, basic_stat_set, zero_stat_set
    ):
        # Case: Base=100, Level=100, Adamant (+Atk, -SpA)
        # Atk: 205 * 1.1 = 225.5 -> 225
        stats = SoulStats(
            base=basic_stat_set,
            ivs=zero_stat_set,
            evs=zero_stat_set,
            level=100,
            nature=Nature.ADAMANT,
        )
        assert stats.attack == 225
        # SpA: 205 * 0.9 = 184.5 -> 184
        assert stats.sp_atk == 184

    def test_hp_shedinja_exception(self, zero_stat_set):
        # Base HP 1 should result in Max HP 1 (Shedinja rule)
        base = StatSet(hp=1)
        stats = SoulStats(
            base=base,
            ivs=zero_stat_set,
            evs=zero_stat_set,
            level=50,
            nature=Nature.HARDY,
        )
        assert stats.max_hp == 1

    def test_serialization(self, basic_stat_set, zero_stat_set):
        stats = SoulStats(
            base=basic_stat_set,
            ivs=zero_stat_set,
            evs=zero_stat_set,
            level=50,
            nature=Nature.ADAMANT,
        )
        data = stats.to_dict()
        assert data["level"] == 50
        assert data["nature"] == "Adamant"
        assert data["base"]["hp"] == 100

        # Round trip
        # Note: from_dict expects flat format from SQL usually, but let's check class method
        # The class method `from_dict` in source code seems to expect flat dict keys like `stat_hp_base`
        # Let's verify that.

        flat_data = {
            "stat_hp_base": 100,
            "stat_atk_base": 100,
            "level": 50,
            "nature": "Adamant",
        }
        restored = SoulStats.from_dict(flat_data)
        assert restored.base.hp == 100
        assert restored.level == 50
        assert restored.nature == Nature.ADAMANT
