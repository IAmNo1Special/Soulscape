from unittest.mock import MagicMock

import pytest

from client.core.soul.soul import Soul


class TestSoul:
    @pytest.fixture
    def soul(self, mock_network_service):
        # Initialize a soul with default args
        soul = Soul(
            orb_color_rgb=(1.0, 0.0, 0.0),
            aura_color_rgb=(0.0, 1.0, 0.0),
            name="Test Soul",
            owner_id="test_owner",
            local_instance_id="test_owner",
        )
        yield soul
        soul.cleanup()

    def test_initialization(self, soul):
        assert soul.biology.name == "Test Soul"
        assert soul.agent is None  # AI parked; see client/ai/README.md

    def test_update_physics(self, soul):
        # Mock physics update
        soul.physics.update = MagicMock()

        soul.update(1.0 / 60.0)

        soul.physics.update.assert_called_once()

    def test_biology_tick(self, soul):
        # Mock biology methods to verify calls
        soul.biology.decrease_satiety = MagicMock()
        soul.biology.decrease_hydration = MagicMock()
        soul.biology.check_status = MagicMock()

        # Simulate > 1 second (stats decrease every 1s usually)
        # Soul.update_interval is default 1.0s
        soul.update(1.1)

        soul.biology.decrease_satiety.assert_called_once()
        soul.biology.decrease_hydration.assert_called_once()
        soul.biology.check_status.assert_called_once()

    def test_snapshot_creation(self, soul):
        snapshot = soul.create_snapshot()
        assert snapshot["name"] == "Test Soul"
        assert "x" in snapshot
        assert "y" in snapshot
        assert snapshot["hp"] == soul.biology.current_health

    def test_cleanup(self, soul, mock_network_service, mocker):
        mock_log = mocker.patch("client.core.soul.soul.log")
        soul.cleanup()
        mock_log.debug.assert_called_with(
            f"Cleaned up resources for soul: {soul.biology.name}"
        )

    def test_update_from_flat_dict_stats(self, soul):
        """Test updating stats from flat dictionary (Hub format)."""
        # Create flat data imitating Hub format
        flat_data = {
            "stat_hp_base": 120,
            "stat_atk_base": 110,
            "stat_def_base": 90,
            "stat_spa_base": 80,
            "stat_spd_base": 70,
            "stat_spe_base": 60,
            "stat_vis_base": 50,
            # IVs
            "stat_hp_iv": 31,
            "stat_atk_iv": 31,
            # Other biology fields
            "satiety": 45.5,
            "hydration": 55.5,
            "hp": 99,
            "xp": 1000.0,
            "level": 5,
        }

        # Perform update
        soul.update_from_dict(flat_data)

        # Verify Stats
        assert soul.biology.stats.base.hp == 120
        assert soul.biology.stats.base.attack == 110
        assert soul.biology.stats.ivs.hp == 31
        assert soul.biology.stats.ivs.attack == 31

        # Verify Biology
        assert soul.biology.satiety == 45.5
        assert soul.biology.hydration == 55.5
        assert soul.biology.current_health == 99
        assert soul.biology.experience_points == 1000.0
        assert soul.biology.level == 5

    def test_update_from_dict_partial(self, soul):
        """Test partial updates don't crash and update what's present."""
        original_hp_base = soul.biology.stats.base.hp

        # Update only position (legacy behavior preserved)
        soul.update_from_dict({"position": [500, 500]})

        assert soul.physics.target_x == 500
        assert soul.physics.target_y == 500
        # Stats should remain unchanged
        assert soul.biology.stats.base.hp == original_hp_base

    def test_update_from_dict_nested_stats_warning(self, soul, caplog):
        """Test that nested stats dict logs a warning but doesn't crash."""
        nested_data = {
            "stats": {
                "base": {"hp": 200},  # This won't be applied currently
            }
        }

        soul.update_from_dict(nested_data)

        assert "Received nested 'stats' dictionary" in caplog.text
        soul.update_from_dict(nested_data)

        assert "Received nested 'stats' dictionary" in caplog.text
