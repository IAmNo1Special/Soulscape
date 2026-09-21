import threading

import pytest

from ..agents import brain_busy


@pytest.fixture(autouse=True)
def clean_guard():
    brain_busy.reset()
    yield
    brain_busy.reset()


def test_acquire_release_cycle():
    assert brain_busy.acquire("s1") is True
    assert brain_busy.is_busy("s1") is True
    brain_busy.release("s1")
    assert brain_busy.is_busy("s1") is False
    assert brain_busy.acquire("s1") is True


def test_second_acquire_fails_while_held():
    assert brain_busy.acquire("s1") is True
    assert brain_busy.acquire("s1") is False


def test_release_unheld_is_harmless():
    brain_busy.release("ghost")
    assert brain_busy.is_busy("ghost") is False


def test_guard_is_per_soul():
    assert brain_busy.acquire("s1") is True
    assert brain_busy.acquire("s2") is True
    assert brain_busy.is_busy("s1") is True
    assert brain_busy.is_busy("s2") is True


def test_concurrent_acquire_serializes():
    winners: list[str] = []
    barrier = threading.Barrier(8)

    def grab(name: str) -> None:
        barrier.wait()
        if brain_busy.acquire("hot"):
            winners.append(name)

    threads = [threading.Thread(target=grab, args=(f"t{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert winners == [winners[0]]
    assert len(winners) == 1
