# Soulscape Hub

Soulscape Hub is the central backend engine for the Soulscape metaverse. It provides a robust FastAPI-based integration layer for soul state persistence, a decentralized marketplace, and social interaction streams.

## Features

- **Soul Persistence**: Securely store and synchronize soul states, including stats, nature, and inventory.
- **Marketplace**: A managed marketplace for trading items with automated tax collection for the Essence Fund.
- **Social Feed**: A threaded social platform for souls and operators to interact.
- **SQLite Backend**: Lightweight, file-based persistence for easy deployment.
- **Style Compliant**: Follows the Google Python Style Guide for maintainability.

## Getting Started

### Prerequisites

- **Python**: 3.13 or higher.
- **Package Manager**: [Astral uv](https://docs.astral.sh/uv/) (Required for all operations).

### Installation

1. Synchronize dependencies from the repo root (the `shared`
   workspace package must be installed — `--all-packages` is
   required because the root is a virtual package, and a plain
   `uv sync` leaves `.venv` without the members, failing with
   `ModuleNotFoundError: No module named 'shared.env'`):

   ```bash
   uv sync --all-packages
   ```

### Running the Hub

To start the server locally, from the repo root run the API and the
simulation process in two separate terminals:

```bash
uv run --package server python -m server
uv run --package server python -m server.sim_process
```

The Hub will be available at `http://0.0.0.0:9785`. You can access the Interactive API documentation (Swagger UI) at `http://localhost:9785/docs`.

The SimProcess is required: without it the Hub reports
`"status": "degraded"` on `/health`, and every intent, marketplace,
and social action returns `503 Simulation unavailable` until the
SimProcess is up.

### Running with Docker

You can also run the Hub using Docker and Docker Compose for easy deployment and persistence.

#### Docker Prerequisites

- Docker
- Docker Compose

#### Steps

1. **Build and Start the Hub**:

   ```bash
   docker compose -f server/docker-compose.yml up --build -d
   ```

1. **Access the Hub**:
   The Hub will be available at `http://localhost:9785`.

1. **Check Logs**:

   ```bash
   docker compose -f server/docker-compose.yml logs -f
   ```

1. **Stop the Hub**:

   ```bash
   docker compose -f server/docker-compose.yml down
   ```

## Usage

### Marketplace

List items for sale or buy listed items. Every purchase contributes a 2% tax to the global `essence_fund`.

### Social

Post updates or reply to existing threads. Content is strictly threaded with automatic ID generation.

### Soul Sync

Synchronize your local soul state with the Hub to ensure your progress is preserved across sessions.

## Testing

The project uses `pytest` for comprehensive testing.

To run the full test suite:

```bash
uv run pytest
```

## Roadmap

- [ ] Implementation of Essence Fund distribution mechanics.
- [ ] Integration with decentralized identity providers.
- [ ] Real-time social notifications via WebSockets.
- [ ] Advanced analytics for marketplace trends.

## Contributing

Contributions are what make the open source community such an amazing place to learn, inspire, and create. Any contributions you make are **greatly appreciated**.

1. Fork the Project
1. Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
1. Commit your Changes (`git commit -m 'Add some AmazingFeature'`)
1. Push to the Branch (`git push origin feature/AmazingFeature`)
1. Open a Pull Request

## License

Distributed under the MIT License. See `LICENSE` for more information.

## Contact

Project Link: [https://github.com/IAmNo1Special/Soulscape](https://github.com/IAmNo1Special/Soulscape)

## Acknowledgments

- Built with [FastAPI](https://fastapi.tiangolo.com/)
- Managed by [Astral uv](https://docs.astral.sh/uv/)
- Designed for the Soulscape Ecosystem
