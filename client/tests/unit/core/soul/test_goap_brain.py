import pytest

from client.core.commands import DrinkCommand, EatCommand, InventoryCommand
from client.core.interactions import Drink, Food
from client.core.soul.goap_brain import GoapBrain
from client.core.soul.soul import Soul


@pytest.fixture
def soul(mock_network_service):
    soul = Soul(
        orb_color_rgb=(1.0, 0.0, 0.0),
        aura_color_rgb=(0.0, 1.0, 0.0),
        name="Brain Test Soul",
        owner_id="test_owner",
        local_instance_id="test_owner",
    )
    yield soul
    soul.cleanup()


@pytest.fixture
def hungry_soul(soul):
    soul.biology.satiety = 20.0
    soul.biology.hydration = 80.0
    return soul


def drain(soul):
    while not soul.command_queue.empty():
        soul.command_queue.get().execute(soul)


class TestGoapBrain:
    def test_local_soul_gets_brain(self, soul):
        assert isinstance(soul.agent, GoapBrain)

    def test_remote_soul_gets_no_brain(self, mock_network_service):
        soul = Soul(
            orb_color_rgb=(1.0, 0.0, 0.0),
            aura_color_rgb=(0.0, 1.0, 0.0),
            name="Remote",
            owner_id="other_owner",
            local_instance_id="test_owner",
        )
        try:
            assert soul.agent is None
        finally:
            soul.cleanup()

    def test_content_soul_idles(self, soul):
        assert soul.agent.trigger_decision(10.0) == "idle"
        assert soul.command_queue.empty()

    def test_hungry_with_food_plans_eat(self, hungry_soul):
        hungry_soul.inventory.add_item(Food("Apple", "Test food.", 20))
        assert hungry_soul.agent.trigger_decision(10.0) == "eat"
        cmd = hungry_soul.command_queue.get()
        assert isinstance(cmd, EatCommand)
        cmd.execute(hungry_soul)
        assert hungry_soul.biology.satiety == 40.0

    def test_thirsty_with_drink_plans_drink(self, soul):
        soul.biology.satiety = 80.0
        soul.biology.hydration = 20.0
        soul.inventory.add_item(Drink("Water", "Test drink.", 25))
        assert soul.agent.trigger_decision(10.0) == "drink"
        cmd = soul.command_queue.get()
        assert isinstance(cmd, DrinkCommand)
        cmd.execute(soul)
        assert soul.biology.hydration == 45.0

    def test_hungry_without_food_plans_find(self, hungry_soul, mocker):
        mocker.patch("random.random", return_value=0.0)
        mocker.patch("random.randint", return_value=20)
        assert hungry_soul.agent.trigger_decision(10.0) == "find_food"
        cmd = hungry_soul.command_queue.get()
        assert isinstance(cmd, InventoryCommand)
        cmd.execute(hungry_soul)
        assert any(isinstance(i, Food) for i in hungry_soul.inventory.items)

    def test_find_food_can_fail(self, hungry_soul, mocker):
        mocker.patch("random.random", return_value=0.99)
        assert hungry_soul.agent.trigger_decision(10.0) == "find_food"
        assert hungry_soul.command_queue.empty()

    def test_tick_drives_brain(self, hungry_soul):
        hungry_soul.inventory.add_item(Food("Apple", "Test food.", 20))
        hungry_soul.agent.decision_interval = 0.1
        hungry_soul.time = 1.0
        hungry_soul.update(0.01)
        assert hungry_soul.biology.satiety > 20.0

    def test_brain_never_raises(self, hungry_soul, mocker):
        mocker.patch.object(
            hungry_soul.agent.planner,
            "generate_plan",
            side_effect=RuntimeError("boom"),
        )
        assert hungry_soul.agent.trigger_decision(10.0) == "error"

    def test_stop_is_noop(self, soul):
        assert soul.agent.stop() is None
