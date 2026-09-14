# Persistent world topology and land allocation

The World is **one persistent coordinate space simulated by the Hub**, existing whether or not any Tamer is connected. Territory is claimed as **Regions**: square plots allocated on an expanding grid, filled ring-by-ring outward from the world origin, ties within a ring broken by signup order. Travel is ordinary movement through shared coordinates; a connected Tamer's screen is a viewport onto their claim.

Resource spawning is Hub-authoritative: items and Essence sources exist in persistent space and are seeded and managed by the Hub, feeding the find-and-sell faucets of the zero-start economy.

This supersedes three earlier decisions from the same design session: the World as the ephemeral union of online screens, signup-order adjacency rings, and the return-home-on-host-disconnect rule (obsolete once hosts stopped existing under ADR-0001).

## Considered Options

- **Origin spiral allocation**: rejected — territories form a snake; neighbour relations become unclear.
- **Ephemeral union-of-screens World**: rejected — contradicts the always-vulnerable ARK-style vision that motivates ADR-0001.
