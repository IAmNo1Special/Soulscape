# Soulscape: Immediate TODOs

To help you progress without feeling overwhelmed, here are the next 3 immediate, small tasks. Focus on these one at a time.

## 1. Populate Client README 📄

The \[client/README.md\](file:///g:/projects/Soulscape/client/README.md) is currently missing or minimal. Use the information in \[.protel/PROJECT_OVERVIEW.md\](file:///g:/projects/Soulscape/.protel/PROJECT_OVERVIEW.md) to create a basic "How to Run" guide for the overlay.

## 2. Consolidate Shared Models 🧩

Identify a single data structure (like `SoulStats`) used in both `client/core/soul.py` and `server/models.py`. Move it to the \[shared\](file:///g:/projects/Soulscape/shared/) directory and update imports. This reduces code duplication.

## 3. Implement a Hub Health Check 🏥

Add a simple `GET /health` endpoint to \[server/main.py\](file:///g:/projects/Soulscape/server/main.py) that returns `{"status": "online"}`. This is a quick win to practice adding routes to the FastAPI backend.

______________________________________________________________________

**Tip**: When you're ready to start one of these, ask me to "Help me with TODO #X" and I'll walk you through it!
