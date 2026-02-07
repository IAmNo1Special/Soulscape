import os
import sys
import threading
import time
import unittest

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from Isekai import HUMAN_FEMALE, HUMAN_MALE, HUMAN_SPECIES, Soul


class TestIsekaiLogic(unittest.TestCase):
    def setUp(self):
        # Prevent threads from running forever during tests if possible
        pass

    def test_soul_creation_defaults(self):
        """Test creating a soul with minimal arguments"""
        soul = Soul()
        self.assertIsNotNone(soul.species)
        self.assertIsNotNone(soul.gender)
        self.assertIsNone(soul.first_name)
        self.assertIsNone(soul.family_name)
        self.assertEqual(soul.get_full_name(), f"Soul #{soul.soul_id}")
        self.assertIsNotNone(soul.hometown)
        self.assertIsNotNone(soul.birth_datetime)

        # Stop the thread
        soul.stop()

    def test_soul_creation_explicit(self):
        """Test creating a soul with explicit arguments"""
        soul = Soul(
            species=HUMAN_SPECIES,
            name="John Doe",
            gender=HUMAN_MALE,
            current_location="Test City",
        )
        self.assertEqual(soul.first_name, "John")
        self.assertEqual(soul.family_name, "Doe")
        self.assertEqual(soul.gender.gender_name, "male")
        self.assertEqual(soul.hometown, "Test City")

        soul.stop()

    def test_stats_initialization(self):
        """Test that stats are initialized"""
        soul = Soul()
        self.assertTrue(soul.max_health > 0)
        self.assertTrue(soul.attack_stat > 0)
        self.assertTrue(soul.defense_stat > 0)
        soul.stop()

    def test_birth_logic(self):
        """Test giving birth"""
        mother = Soul(
            species=HUMAN_SPECIES,
            name="Mom",
            gender=HUMAN_FEMALE,
            current_location="Home",
        )
        father = Soul(species=HUMAN_SPECIES, name="Dad", gender=HUMAN_MALE)

        child = mother.give_birth()
        self.assertIsNotNone(child)
        self.assertEqual(child.birth_mother, mother)
        self.assertEqual(child.hometown, "Home")  # Should inherit location

        # Name the child
        father.name_child(child)
        self.assertIsNotNone(child.first_name)
        self.assertEqual(
            child.family_name, "Doe"
        )  # Default if Father has no family name or "Dad" isn't split?
        # Wait, Dad name was "Dad", so family name is "Doe" by default in my logic?
        # let's check Dad's family name
        self.assertEqual(father.family_name, "Doe")
        self.assertEqual(child.family_name, "Doe")

        mother.stop()
        father.stop()
        child.stop()

    def test_attack_logic(self):
        """Test combat"""
        attacker = Soul(name="Attacker")
        defender = Soul(name="Defender")

        initial_hp = defender.current_health
        damage = attacker.attack(defender)

        self.assertIsNotNone(damage)
        self.assertTrue(defender.current_health < initial_hp or damage == 0)

        attacker.stop()
        defender.stop()

    def test_thread_safety_mock(self):
        """Simple check that update loop runs"""
        soul = Soul()
        time.sleep(1.1)
        # Hunger should have decreased
        self.assertTrue(soul.hunger < 100)
        soul.stop()


if __name__ == "__main__":
    unittest.main()
