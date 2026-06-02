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


## Persistent Rooms

Rooms are saved as JSON snapshots after create/join/reconnect and after successful game actions.

By default, snapshots are written to `room_state/`. You can override that location with:

```powershell
$env:POKER_STATE_DIR="G:\Poker Online\room_state"
```

On Render, the normal filesystem is ephemeral. For rooms to survive Render restarts and deploys reliably, use a paid web service with a persistent disk and set `POKER_STATE_DIR` to a folder on that disk, such as `/data/room_state`.


## Room Passwords

When creating a room, enter a room password and share it privately with friends. Joining a protected room requires that password unless the browser already has a valid reconnect token for a player in that room.

Passwords are stored as hashes in room snapshots. Existing rooms without a password remain open until recreated with a password.


## Player Passwords

Use the player password field as a per-name PIN. If a player name already exists in a room, another browser must provide the matching player password to reclaim that player. A browser with the original reconnect token can still reconnect without retyping the player password.

Player passwords are stored as hashes in room snapshots. Existing players without a password can claim/set one the next time they rejoin with a player password.


## Straddle Rule

Straddles are treated like posted blind money before the preflop betting round. After the straddle option ends, preflop action starts after the last straddler, so the last straddler receives the final preflop option if nobody raises.


## Login And Lobby

The root page now starts with a login screen. The username becomes the poker player name, and the login password is reused as that player's password/PIN.

After login, the lobby shows current rooms, lets players refresh the room list, join a room, or create a named room with a room password. Creating or joining a room opens the poker table view.
