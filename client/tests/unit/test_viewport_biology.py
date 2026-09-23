"""Step-0 tests for issue #30: the viewport streams satiety/hydration/hp
and the client applies them to the local biology model so
state_to_uniforms() gets real authoritative values online.

Plus the golden uniform test with streamed values: the fractions the
app derives from the stream (satiety/100, hydration/100, hp/max_hp)
must produce the exact uniforms hand-derived from the documented
curves in client/ui/graphics/soul_uniforms.py.
"""

from __future__ import annotations

import unittest

from shared import protocol

from client.system.network.viewport_client import ViewportConsumer
from client.ui.graphics.soul_uniforms import state_to_uniforms


def _snapshot(soul_id, **bio):
    entry = {"soul_id": soul_id, "x": 10.0, "y": 20.0}
    entry.update(bio)
    return {
        "type": protocol.MessageType.SNAPSHOT.value,
        "souls": [entry],
        "wallets": [{"soul_id": soul_id, "essence": 42.0}],
    }


def _delta(ops):
    return {"type": protocol.MessageType.DELTA.value, "ops": ops}


class TestConsumerBiology(unittest.TestCase):
    def test_snapshot_biology_applied(self):
        consumer = ViewportConsumer(clock=lambda: 1000.0)
        consumer.apply_frame(
            _snapshot("s1", satiety=30.0, hydration=40.0, hp=50.0, max_hp=120.0)
        )
        self.assertEqual(
            consumer.soul_biology("s1"),
            {"satiety": 30.0, "hydration": 40.0, "hp": 50.0, "max_hp": 120.0},
        )

    def test_snapshot_defaults_when_absent(self):
        consumer = ViewportConsumer(clock=lambda: 1000.0)
        consumer.apply_frame(_snapshot("s1"))
        self.assertEqual(
            consumer.soul_biology("s1"),
            {"satiety": 100.0, "hydration": 100.0, "hp": 100.0, "max_hp": 100.0},
        )

    def test_biology_delta_domain_updates(self):
        consumer = ViewportConsumer(clock=lambda: 1000.0)
        consumer.apply_frame(_snapshot("s1"))
        consumer.apply_frame(
            _delta(
                [
                    {
                        "op": protocol.EntityOpKind.UPSERT.value,
                        "soul_id": "s1",
                        "domain": "biology",
                        "state": {
                            "soul_id": "s1",
                            "satiety": 11.0,
                            "hydration": 22.0,
                            "hp": 33.0,
                            "max_hp": 44.0,
                        },
                    }
                ]
            )
        )
        self.assertEqual(
            consumer.soul_biology("s1"),
            {"satiety": 11.0, "hydration": 22.0, "hp": 33.0, "max_hp": 44.0},
        )

    def test_remove_cleans_biology(self):
        consumer = ViewportConsumer(clock=lambda: 1000.0)
        consumer.apply_frame(_snapshot("s1", satiety=5.0))
        consumer.apply_frame(
            _delta(
                [
                    {
                        "op": protocol.EntityOpKind.REMOVE.value,
                        "soul_id": "s1",
                        "state": None,
                    }
                ]
            )
        )
        self.assertEqual(
            consumer.soul_biology("s1"),
            {"satiety": 100.0, "hydration": 100.0, "hp": 100.0, "max_hp": 100.0},
        )

    def test_unknown_soul_reads_healthy_defaults(self):
        consumer = ViewportConsumer(clock=lambda: 1000.0)
        self.assertEqual(consumer.soul_biology("ghost")["satiety"], 100.0)


class TestConsumerWallets(unittest.TestCase):
    def test_snapshot_wallets(self):
        consumer = ViewportConsumer(clock=lambda: 1000.0)
        consumer.apply_frame(_snapshot("s1"))
        self.assertEqual(consumer.soul_essence("s1"), 42.0)
        self.assertEqual(consumer.wallets(), {"s1": 42.0})

    def test_economy_delta_updates_wallet(self):
        consumer = ViewportConsumer(clock=lambda: 1000.0)
        consumer.apply_frame(_snapshot("s1"))
        consumer.apply_frame(
            _delta(
                [
                    {
                        "op": protocol.EntityOpKind.UPSERT.value,
                        "soul_id": "s1",
                        "domain": "economy",
                        "state": {"soul_id": "s1", "essence": 40.0},
                    }
                ]
            )
        )
        self.assertEqual(consumer.soul_essence("s1"), 40.0)

    def test_unknown_wallet_is_none(self):
        consumer = ViewportConsumer(clock=lambda: 1000.0)
        self.assertIsNone(consumer.soul_essence("ghost"))


class TestConsumerBubbles(unittest.TestCase):
    def test_bubble_ops_queued_and_drained(self):
        consumer = ViewportConsumer(clock=lambda: 1000.0)
        consumer.apply_frame(_snapshot("s1"))
        consumer.apply_frame(
            _delta(
                [
                    {
                        "op": "bubble",
                        "soul_id": "s1",
                        "text": "a quip!",
                        "kind": "quip",
                        "solicited": True,
                    }
                ]
            )
        )
        drained = consumer.drain_bubbles()
        self.assertEqual(len(drained), 1)
        self.assertEqual(drained[0]["text"], "a quip!")
        self.assertEqual(drained[0]["kind"], "quip")
        self.assertTrue(drained[0]["solicited"])
        self.assertEqual(consumer.drain_bubbles(), [])

    def test_bubble_does_not_touch_entity_state(self):
        consumer = ViewportConsumer(clock=lambda: 1000.0)
        consumer.apply_frame(_snapshot("s1"))
        consumer.apply_frame(
            _delta([{"op": "bubble", "soul_id": "s1", "text": "hi"}])
        )
        self.assertEqual(consumer.soul_state("s1"), "normal")
        self.assertEqual(consumer.soul_biology("s1")["satiety"], 100.0)


class TestStreamedGoldenUniforms(unittest.TestCase):
    """The app turns streamed biology into fractions for _soul_state():
    satiety/100, hydration/100, hp/max_hp. With satiety=50,
    hydration=60, hp=70, max_hp=100 the documented curves give:

        vitality       = 0.40*0.5 + 0.35*0.6 + 0.25*0.7 = 0.585
        pulse_rate     = 1.0 + 1.8*(1-0.585) = 1.747
        pulse_strength = 0.25 + 0.75*0.585 = 0.68875
        brightness     = 0.45 + 0.55*0.585 = 0.77175
        no tint -> base color unchanged
    """

    def test_streamed_values_drive_uniforms(self):
        consumer = ViewportConsumer(clock=lambda: 1000.0)
        consumer.apply_frame(
            _snapshot("s1", satiety=50.0, hydration=60.0, hp=70.0, max_hp=100.0)
        )
        bio = consumer.soul_biology("s1")
        state = {
            "satiety": bio["satiety"] / 100.0,
            "hydration": bio["hydration"] / 100.0,
            "hp": bio["hp"] / max(1.0, bio["max_hp"]),
            "statue_kind": None,
            "typing_dip": 0.0,
            "reflex": None,
            "reflex_t": 0.0,
            "base_color": (0.9, 0.2, 0.3),
        }
        uniforms = state_to_uniforms(state)
        self.assertEqual(uniforms["pulse_rate"], 1.747)
        self.assertEqual(uniforms["pulse_strength"], 0.68875)
        self.assertEqual(uniforms["brightness"], 0.77175)
        self.assertEqual(
            uniforms["base_color_uniform"], (0.9, 0.2, 0.3)
        )
        self.assertEqual(uniforms["desat_factor"], 0.0)
        self.assertEqual(uniforms["opacity"], 1.0)
        self.assertEqual(uniforms["opacity"], 1.0)


if __name__ == "__main__":
    unittest.main()
