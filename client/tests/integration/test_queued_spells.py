import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from client.core import Drink, Food
from client.core.commands import (
    BuyItemCommand,
    CancelListingCommand,
    DrinkCommand,
    EatCommand,
    MoveCommand,
    SellItemCommand,
)
from client.core.soul.soul import Soul


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
    soul.cleanup()


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

    # Atomic Eat: Should have ONE command now
    assert soul.command_queue.qsize() == 1
    cmd = soul.command_queue.get()

    assert isinstance(cmd, EatCommand)
    assert cmd.item == mock_food
    soul.cleanup()


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

    # Atomic Drink: Should have ONE command now
    assert soul.command_queue.qsize() == 1
    cmd = soul.command_queue.get()

    assert isinstance(cmd, DrinkCommand)
    assert cmd.item == mock_drink
    soul.cleanup()


@pytest.mark.asyncio
async def test_market_sell_enqueues_command(
    mock_network_service, mock_grimorium
):
    soul = Soul(
        orb_color_rgb=(1, 0, 0),
        aura_color_rgb=(0, 1, 0),
        owner_id="test",
        local_instance_id="test",
    )
    # Dynamically load market_sell
    path = (
        Path(__file__).parent.parent.parent
        / "core"
        / "soul"
        / ".magetools"
        / "economy"
        / "marketplace.py"
    )
    spec = importlib.util.spec_from_file_location("marketplace", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    market_sell = module.market_sell

    mock_item = Food("Rare Gem", "Very shiny.", 10)
    soul.inventory.add_item(mock_item)

    await market_sell(soul, item_index=0, price=100.0)

    assert not soul.command_queue.empty()
    cmd = soul.command_queue.get()
    assert isinstance(cmd, SellItemCommand)
    assert cmd.item_index == 0
    assert cmd.price == 100.0
    soul.cleanup()


@pytest.mark.asyncio
async def test_market_buy_enqueues_command(
    mock_network_service, mock_grimorium
):
    soul = Soul(
        orb_color_rgb=(1, 0, 0),
        aura_color_rgb=(0, 1, 0),
        owner_id="test",
        local_instance_id="test",
    )
    # Dynamically load market_buy
    path = (
        Path(__file__).parent.parent.parent
        / "core"
        / "soul"
        / ".magetools"
        / "economy"
        / "marketplace.py"
    )
    spec = importlib.util.spec_from_file_location("marketplace", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    market_buy = module.market_buy

    await market_buy(soul, listing_id="listing_123")

    assert not soul.command_queue.empty()
    cmd = soul.command_queue.get()
    assert isinstance(cmd, BuyItemCommand)
    assert cmd.listing_id == "listing_123"
    soul.cleanup()


@pytest.mark.asyncio
async def test_market_cancel_enqueues_command(
    mock_network_service, mock_grimorium, mocker
):
    soul = Soul(
        orb_color_rgb=(1, 0, 0),
        aura_color_rgb=(0, 1, 0),
        owner_id="test",
        local_instance_id="test",
    )
    # Dynamically load market_cancel
    path = (
        Path(__file__).parent.parent.parent
        / "core"
        / "soul"
        / ".magetools"
        / "economy"
        / "marketplace.py"
    )
    spec = importlib.util.spec_from_file_location("marketplace", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    market_cancel = module.market_cancel

    # Mock Marketplace to allow cancellation (ownership check)
    mock_listing = MagicMock()
    mock_listing.seller_id = soul.biology.soul_id
    mocker.patch(
        "client.core.interactions.Marketplace.get_listing",
        return_value=mock_listing,
    )

    await market_cancel(soul, listing_id="listing_123")

    assert not soul.command_queue.empty()
    cmd = soul.command_queue.get()
    assert isinstance(cmd, CancelListingCommand)
    assert cmd.listing_id == "listing_123"
    soul.cleanup()
