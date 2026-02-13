from unittest.mock import MagicMock

from soulscape.core.commands import (
    BuyItemCommand,
    CancelListingCommand,
    EssenceCommand,
    InventoryCommand,
    MoveCommand,
    OwnerPresenceCommand,
    SellItemCommand,
    SpeakCommand,
    StateUpdateCommand,
    VitalCommand,
)


class MockSoul:
    def __init__(self):
        self.biology = MagicMock()
        self.biology.name = "TestSoul"
        self.biology.satiety = 50.0
        self.biology.hydration = 50.0
        self.biology.current_health = 100.0
        self.biology.stats.max_hp = 100.0
        self.physics = MagicMock()
        self.inventory = MagicMock()
        self.inventory.items = []
        self.inventory.capacity = 100
        self.marketplace = MagicMock()
        self.essence = 100.0
        self.soul_registry = []
        self.soul_id = "test_sid"
        self.local_instance_id = "test_sid"


def test_move_command():
    soul = MockSoul()
    cmd = MoveCommand(x=10.0, y=20.0)
    cmd.execute(soul)
    assert soul.physics.target_location == (10.0, 20.0)


def test_speak_command(mocker):
    soul = MockSoul()
    mock_log = mocker.patch("soulscape.core.commands.log")
    cmd = SpeakCommand(message="Hello")
    cmd.execute(soul)
    mock_log.info.assert_called_with("[SOUL SPEAK] TestSoul: Hello")


def test_inventory_command_add():
    soul = MockSoul()
    mock_item = MagicMock()
    mock_item.name = "TestItem"
    cmd = InventoryCommand(action="add", item=mock_item)
    cmd.execute(soul)
    soul.inventory.add_item.assert_called_with(mock_item)


def test_vital_command_satiety():
    soul = MockSoul()
    soul.biology.satiety = 50.0
    cmd = VitalCommand(vital_type="satiety", amount=10.0)
    cmd.execute(soul)
    assert soul.biology.satiety == 60.0


def test_vital_command_limits():
    soul = MockSoul()
    soul.biology.satiety = 95.0
    cmd = VitalCommand(vital_type="satiety", amount=10.0)
    cmd.execute(soul)
    assert soul.biology.satiety == 100.0


def test_essence_command():
    soul = MockSoul()
    soul.essence = 100.0
    cmd = EssenceCommand(amount=50.5)
    cmd.execute(soul)
    assert soul.essence == 150.5


def test_state_update_command():
    soul = MockSoul()
    soul.local_instance_id = "local_sid"
    remote_soul = MockSoul()
    remote_soul.biology.soul_id = "target_sid"
    remote_soul.owner_id = "test_owner"
    soul.soul_registry = [remote_soul]

    data = {"soul_id": "target_sid", "position": [10, 20]}
    cmd = StateUpdateCommand(owner_id="test_owner", souls_data=[data])

    remote_soul.update_from_dict = MagicMock()
    cmd.execute(soul)
    remote_soul.update_from_dict.assert_called_with(data)


def test_state_update_command_spoofing_protection(mocker):
    soul = MockSoul()
    soul.local_instance_id = "local_sid"
    mock_log = mocker.patch("soulscape.core.commands.log")

    data = {"soul_id": "local_sid", "position": [10, 20]}
    cmd = StateUpdateCommand(owner_id="remote_attacker", souls_data=[data])

    cmd.execute(soul)
    mock_log.warning.assert_called()
    assert "Prevented spoofing attempt" in mock_log.warning.call_args[0][0]


def test_owner_presence_command_offline():
    soul = MockSoul()
    s1 = MockSoul()
    s1.owner_id = "owner_a"
    s2 = MockSoul()
    s2.owner_id = "owner_b"
    soul.soul_registry = [s1, s2]

    cmd = OwnerPresenceCommand(owner_id="owner_a", action="offline")
    cmd.execute(soul)
    assert len(soul.soul_registry) == 1
    assert soul.soul_registry[0].owner_id == "owner_b"


def test_sell_item_command():
    soul = MockSoul()
    mock_item = MagicMock()
    mock_item.name = "TestItem"
    soul.inventory.items = [mock_item]

    cmd = SellItemCommand(item_index=0, price=50.0)
    cmd.execute(soul)

    assert len(soul.inventory.items) == 0
    soul.marketplace.list_item.assert_called_with(mock_item, 50.0, soul.soul_id)


def test_buy_item_command():
    soul = MockSoul()
    soul.essence = 100.0
    mock_item = MagicMock()
    mock_item.name = "TestItem"
    mock_listing = MagicMock()
    mock_listing.price = 50.0
    mock_listing.item = mock_item

    soul.marketplace.get_listing.return_value = mock_listing
    soul.marketplace.remove_listing.return_value = mock_item

    cmd = BuyItemCommand(listing_id="listing_123")
    cmd.execute(soul)

    assert soul.essence == 50.0
    soul.inventory.add_item.assert_called_with(mock_item)


def test_cancel_listing_command():
    soul = MockSoul()
    mock_item = MagicMock()
    mock_item.name = "TestItem"
    mock_listing = MagicMock()
    mock_listing.item = mock_item

    soul.marketplace.get_listing.return_value = mock_listing
    soul.marketplace.remove_listing.return_value = mock_item

    cmd = CancelListingCommand(listing_id="listing_123")
    cmd.execute(soul)

    soul.inventory.add_item.assert_called_with(mock_item)
