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
- **Package Manager**: [Astral uv](https://docs.astral.sh/uv/) (Recommended for all operations).

### Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/IAmNo1Special/soulscape_hub.git
   cd soulscape_hub
   ```

2. Synchronize dependencies:
   ```bash
   uv sync
   ```

### Running the Hub

To start the server locally:

```bash
uv run main.py
```

The Hub will be available at `http://0.0.0.0:8000`. You can access the Interactive API documentation (Swagger UI) at `http://localhost:8000/docs`.

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
2. Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
3. Commit your Changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the Branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## License

Distributed under the MIT License. See `LICENSE` for more information.

## Contact

Project Link: [https://github.com/IAmNo1Special/soulscape_hub](https://github.com/IAmNo1Special/soulscape_hub)

## Acknowledgments

- Built with [FastAPI](https://fastapi.tiangolo.com/)
- Managed by [Astral uv](https://docs.astral.sh/uv/)
- Designed for the Soulscape Ecosystem
