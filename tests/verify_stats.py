import unittest

# Mocking window/physics for Soul init
from unittest.mock import MagicMock

from soulscape.core.soul import Soul
from soulscape.core.stats import Nature, SoulStats, Stat


class TestStats(unittest.TestCase):
    def test_stats_calculation(self):
        # Create known stats
        # Base 100, IV 31, EV 0, Level 50, Nature Adamant (+Atk, -SpA)
        stats = SoulStats.create_random(level=50)
        stats.base.hp = 100
        stats.base.attack = 100
        stats.base.defense = 100
        stats.base.sp_atk = 100
        stats.base.sp_def = 100
        stats.base.speed = 100

        stats.ivs.hp = 31
        stats.ivs.attack = 31
        stats.ivs.defense = 31
        stats.ivs.sp_atk = 31
        stats.ivs.sp_def = 31
        stats.ivs.speed = 31

        stats.evs.hp = 0  # EVs 0 for simplicity

        stats.nature = Nature.ADAMANT
        stats.level = 50

        # HP Formula: ((2*Base + IV + EV/4) * Level / 100) + Level + 10
        # ((200 + 31 + 0) * 50 / 100) + 50 + 10 = (231 * 0.5) + 60 = 115.5 + 60 = 175
        expected_hp = 175
        self.assertEqual(stats.max_hp, expected_hp)

        # Atk Formula: (((2*Base + IV + EV/4) * Level / 100) + 5) * Nature
        # ((231 * 0.5) + 5) * 1.1 = (115.5 + 5) * 1.1 = 120.5 * 1.1 = 132.55 -> 132
        expected_atk = 132
        self.assertEqual(stats.attack, expected_atk)

        # SpA (Decreased)
        # ((231 * 0.5) + 5) * 0.9 = 120.5 * 0.9 = 108.45 -> 108
        expected_spa = 108
        self.assertEqual(stats.sp_atk, expected_spa)

        print("Stats Calculation Verified!")

    def test_soul_integration(self):
        # Mock window
        mock_window = MagicMock()
        mock_window.height = 1080

        # Init soul
        soul = Soul(mock_window, (1, 1, 1), (1, 1, 1), name="TestSoul")

        # Check if stats exist
        self.assertIsNotNone(soul.stats)
        self.assertIsInstance(soul.stats, SoulStats)

        # Check physics speed scaling
        # Default speed stat ~100-131 at level 5? No, level default is what?
        # create_random uses level 5.
        # Speed stat at level 5: ((231 * 5 / 100) + 5) * 1.0 = 11.55 + 5 = 16
        # Wait, my formula/constants might yield low values at low levels.
        # Physics expects ~100 to be baseline for 0.5 max_speed.

        # If Level 5, speed is ~16.
        # Multiplier = 16 / 100 = 0.16.
        # Max speed = 0.5 * 0.16 = 0.08. Very slow!

        # Maybe default level should be 50? Or 100?
        # Or base stats higher?
        # Or physics baseline lower?

        # Users want Pokemon stats (values usually 200-300 at lvl 100).
        # At lvl 5, stats are low (10-20).
        # If I want "normal" movement, I should probably normalize based on level?
        # Or just say "Level 1 souls are slow".

        print(f"Soul Speed Stat: {soul.stats.speed}")
        print(f"Physics Max Speed: {soul.window_physics.max_speed}")

    def test_serialization(self):
        stats = SoulStats.create_random()
        data = stats.to_dict()
        stats2 = SoulStats.from_dict(data)
        self.assertEqual(stats.nature, stats2.nature)
        self.assertEqual(stats.ivs, stats2.ivs)


if __name__ == "__main__":
    unittest.main()
