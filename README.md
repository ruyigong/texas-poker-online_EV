# Texas Poker Online

FastAPI prototype for an online Texas Hold'em game.

Current milestone:

- Create a private room code
- Join an existing room code
- Keep each room's WebSocket traffic separate
- Track players in the room
- Start a simple shared poker table state with shuffled hole cards, community cards, pot, blinds, and dealer position

## Run Locally

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000` in two browser tabs. Create a room in one tab, then join that room code from the other tab.

## Deploy On Render

This project includes `render.yaml`.

Render start command:

```powershell
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```
