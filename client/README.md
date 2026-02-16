# Soulscape

A desktop-integrated creature simulation featuring autonomous souls with LLM-driven behaviors.

## Soulscape Hub

The Hub serves as the central coordination point for the simulation, providing marketplace, social, and soul persistence services.

### Running with Docker

You can run the Hub using Docker and Docker Compose for easy deployment and persistence.

#### Prerequisites
- Docker
- Docker Compose

#### Steps

1. **Build and Start the Hub**:
   ```bash
   docker compose -f hub/docker-compose.yml up --build -d
   ```

2. **Access the Hub**:
   The Hub will be available at `http://localhost:8000`.

3. **Check Logs**:
   ```bash
   docker compose -f hub/docker-compose.yml logs -f
   ```

4. **Stop the Hub**:
   ```bash
   docker compose -f hub/docker-compose.yml down
   ```

### Manual Run

If you prefer to run it manually using `uv`:

```bash
uv run hub/main.py
```
