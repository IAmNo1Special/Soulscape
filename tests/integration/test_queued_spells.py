import importlib.util
from pathlib import Path

import pytest

from soulscape.core import Drink, Food
from soulscape.core.commands import InventoryCommand, MoveCommand, VitalCommand
from soulscape.core.soul.soul import Soul


@pytest.mark.asyncio
async def test_move_to_enqueues_command(mock_network_service, mock_grimorium):
    soul = Soul(
        orb_color_rgb=(1, 0, 0),
        aura_color_rgb=(0, 1, 0),
        owner_id="test",
        local_instance_id="test",
    )
    # Dynamically load move_to
    path = (
        Path(__file__).parent.parent.parent
        / "src"
        / "soulscape"
        / "core"
        / "soul"
        / ".magetools"
        / "survival"
        / "movement.py"
    )
    spec = importlib.util.spec_from_file_location("movement", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    move_to = module.move_to

    await move_to(soul, 100, 200)

    assert not soul.command_queue.empty()
    cmd = soul.command_queue.get()
    assert isinstance(cmd, MoveCommand)
    assert cmd.x == 100
    assert cmd.y == 200


@pytest.mark.asyncio
async def test_eat_enqueues_commands(mock_network_service, mock_grimorium):
    soul = Soul(
        orb_color_rgb=(1, 0, 0),
        aura_color_rgb=(0, 1, 0),
        owner_id="test",
        local_instance_id="test",
    )
    # Dynamically load eat
    path = (
        Path(__file__).parent.parent.parent
        / "src"
        / "soulscape"
        / "core"
        / "soul"
        / ".magetools"
        / "survival"
        / "satiety.py"
    )
    spec = importlib.util.spec_from_file_location("satiety", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    eat = module.eat

    mock_food = Food("Apple", "Tasty", 20)
    soul.inventory.add_item(mock_food)

    await eat(soul)

    # Should have two commands: remove item and add satiety
    assert soul.command_queue.qsize() == 2
    cmd1 = soul.command_queue.get()
    cmd2 = soul.command_queue.get()

    assert isinstance(cmd1, InventoryCommand)
    assert cmd1.action == "remove"
    assert isinstance(cmd2, VitalCommand)
    assert cmd2.vital_type == "satiety"
    assert cmd2.amount == 20


@pytest.mark.asyncio
async def test_drink_enqueues_commands(mock_network_service, mock_grimorium):
    soul = Soul(
        orb_color_rgb=(1, 0, 0),
        aura_color_rgb=(0, 1, 0),
        owner_id="test",
        local_instance_id="test",
    )
    # Dynamically load drink
    path = (
        Path(__file__).parent.parent.parent
        / "src"
        / "soulscape"
        / "core"
        / "soul"
        / ".magetools"
        / "survival"
        / "hydration.py"
    )
    spec = importlib.util.spec_from_file_location("hydration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    drink = module.drink

    mock_drink = Drink("Water", "Clear", 15)
    soul.inventory.add_item(mock_drink)

    await drink(soul)

    assert soul.command_queue.qsize() == 2
    cmd1 = soul.command_queue.get()
    cmd2 = soul.command_queue.get()

    assert isinstance(cmd1, InventoryCommand)
    assert cmd1.action == "remove"
    assert isinstance(cmd2, VitalCommand)
    assert cmd2.vital_type == "hydration"
    assert cmd2.amount == 15
