# Hub-authoritative persistent simulation

Soulscape's thesis is a game-world frontend for AI agents where Souls live, work, and defend territory while their Tamers are offline (inspired by ARK/PalWorld persistence and DayZ's authoritative servers). We decided that **all Soul simulation — physics, biology, and LLM agent decisions — runs server-side in the Hub around the clock**, regardless of client connectivity; clients degrade to viewports and input devices, and world-state streams Hub→client rather than client→Hub.

Inference runs at the Hub: initially against per-Tamer API keys uploaded and encrypted at rest, spent only on their own Souls; the target state meters token usage into Essence debited from Soul wallets into the Essence Fund. A Soul that cannot pay goes **Dormant**; a daily non-accumulating thought allowance keeps newborn Souls alive without injecting wealth (DayZ-style zero-start economy stands).

## Considered Options

- **Origin-authoritative streaming** (today's architecture): rejected — a Soul dies whenever its Tamer disconnects, betraying the core thesis.
- **Host-authoritative visiting**: rejected — requires sharing Soul credentials with strangers' machines, and leaves home-region Souls frozen when their own Tamer quits (the asymmetry that triggered this decision).
