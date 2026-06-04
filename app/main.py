import hashlib
import json
import os
import random
import re
import string
import time
from datetime import datetime
from pathlib import Path
from collections import Counter
from dataclasses import dataclass, field, fields
from itertools import combinations
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Texas Poker Online")

SUITS = ["S", "H", "D", "C"]
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
RANK_VALUES = {rank: index + 2 for index, rank in enumerate(RANKS)}
STARTING_CHIPS = 1000
SMALL_BLIND = 5
BIG_BLIND = 5
MAX_SEATS = 10
DEFAULT_ACTION_TIMEOUT = 15
BETTING_STAGES = ["preflop", "flop", "turn", "river"]
APP_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HISTORY_DIR = "hand_history_online" if os.environ.get("RENDER") else "hand_history"
HISTORY_ROOT = Path(os.environ.get("POKER_HISTORY_ROOT", APP_ROOT / DEFAULT_HISTORY_DIR))
ROOM_STATE_ROOT = Path(os.environ.get("POKER_STATE_DIR", APP_ROOT / "room_state"))
ROOM_STATE_VERSION = 1
INVITE_CODE = os.environ.get("POKER_INVITE_CODE", "evanston")
AUDIO_ROOT = APP_ROOT / "audios"
PICTURE_ROOT = APP_ROOT / "pictures"
if AUDIO_ROOT.exists():
    app.mount("/audios", StaticFiles(directory=AUDIO_ROOT), name="audios")
if PICTURE_ROOT.exists():
    app.mount("/pictures", StaticFiles(directory=PICTURE_ROOT), name="pictures")


def password_hash(password: str) -> str:
    password = password.strip()
    if not password:
        return ""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def make_room_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=5))


def room_code_from_name(room_name: str) -> str:
    code = re.sub(r"[^A-Za-z0-9_-]+", "-", room_name.strip()).strip("-_")
    return code[:32].upper()


def history_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip()).strip("-_")
    return slug[:48] or "room"


def history_file_path(hand_id: str) -> Path | None:
    safe_id = history_slug(hand_id)
    if safe_id != hand_id:
        return None
    root = HISTORY_ROOT.resolve()
    path = (root / hand_id / "betting_history.json").resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path

def list_saved_histories(limit: int = 50) -> list[dict[str, Any]]:
    if not HISTORY_ROOT.exists():
        return []
    items = []
    for path in HISTORY_ROOT.glob("*/betting_history.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            payload = {}
        stat = path.stat()
        hand_id = path.parent.name
        items.append({
            "handId": payload.get("handId") or hand_id,
            "roomCode": payload.get("roomCode", ""),
            "roomName": payload.get("roomName", ""),
            "status": payload.get("status", "unknown"),
            "startedAt": payload.get("startedAt"),
            "endedAt": payload.get("endedAt"),
            "updatedAt": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            "url": f"/history/{hand_id}",
            "downloadUrl": f"/history/{hand_id}/download",
        })
    items.sort(key=lambda item: item.get("updatedAt") or "", reverse=True)
    return items[:limit]


def make_deck() -> list[str]:
    deck = [f"{rank}{suit}" for suit in SUITS for rank in RANKS]
    random.shuffle(deck)
    return deck


def rank_of(card: str) -> str:
    return card[:-1]


def suit_of(card: str) -> str:
    return card[-1]


def hand_name(category: int) -> str:
    return {
        8: "Straight flush",
        7: "Four of a kind",
        6: "Full house",
        5: "Flush",
        4: "Straight",
        3: "Three of a kind",
        2: "Two pair",
        1: "Pair",
        0: "High card",
    }[category]


def straight_high(values: list[int]) -> int | None:
    unique_values = sorted(set(values), reverse=True)
    if 14 in unique_values:
        unique_values.append(1)
    for index in range(len(unique_values) - 4):
        window = unique_values[index : index + 5]
        if window[0] - window[4] == 4:
            return window[0]
    return None


def evaluate_five(cards: tuple[str, ...]) -> tuple[int, list[int]]:
    values = sorted((RANK_VALUES[rank_of(card)] for card in cards), reverse=True)
    suits = [suit_of(card) for card in cards]
    counts = Counter(values)
    groups = sorted(counts.items(), key=lambda item: (item[1], item[0]), reverse=True)
    flush = len(set(suits)) == 1
    straight = straight_high(values)
    if flush and straight:
        return 8, [straight]
    if groups[0][1] == 4:
        kicker = max(value for value in values if value != groups[0][0])
        return 7, [groups[0][0], kicker]
    if groups[0][1] == 3 and groups[1][1] == 2:
        return 6, [groups[0][0], groups[1][0]]
    if flush:
        return 5, values
    if straight:
        return 4, [straight]
    if groups[0][1] == 3:
        kickers = sorted((value for value in values if value != groups[0][0]), reverse=True)
        return 3, [groups[0][0], *kickers]
    pairs = sorted((value for value, count in counts.items() if count == 2), reverse=True)
    if len(pairs) == 2:
        kicker = max(value for value in values if value not in pairs)
        return 2, [*pairs, kicker]
    if len(pairs) == 1:
        kickers = sorted((value for value in values if value != pairs[0]), reverse=True)
        return 1, [pairs[0], *kickers]
    return 0, values


def evaluate_best(cards: list[str]) -> tuple[int, list[int]]:
    return max(evaluate_five(combo) for combo in combinations(cards, 5))


@dataclass
class Player:
    id: str
    name: str
    password_hash: str = ""
    chips: int = STARTING_CHIPS
    cards: list[str] = field(default_factory=list)
    connected: bool = True
    is_spectator: bool = False
    wants_seat: bool = False
    folded: bool = False
    all_in: bool = False
    current_bet: int = 0
    total_bet: int = 0
    acted: bool = False
    ready_next_hand: bool = False
    rebuy_units: int = 1
    pending_rebuy_units: int = 0
    refill_submitted: bool = False
    checked_marker: bool = False
    cards_visible: bool = False
    squids: int = 0
    has_played: bool = False

    def can_act(self) -> bool:
        return not self.is_spectator and not self.folded and not self.all_in and self.chips > 0 and bool(self.cards)


@dataclass
class Room:
    code: str
    name: str = ""
    password_hash: str = ""
    players: dict[str, Player] = field(default_factory=dict)
    sockets: dict[str, WebSocket] = field(default_factory=dict)
    deck: list[str] = field(default_factory=list)
    community_cards: list[str] = field(default_factory=list)
    pot: int = 0
    dealer_index: int = 0
    turn_index: int = 0
    current_bet: int = 0
    min_raise: int = BIG_BLIND
    small_blind: int = SMALL_BLIND
    big_blind: int = BIG_BLIND
    action_timeout: int = DEFAULT_ACTION_TIMEOUT
    buy_time_seconds: int = 30
    peek_price_min: int = 2
    peek_price_max: int = 10
    poker_god_chips: int = 0
    private_card_views: dict[str, list[str]] = field(default_factory=dict)
    action_deadline: float | None = None
    straddle_index: int = 0
    last_straddler_index: int | None = None
    straddle_amount: int = 0
    cards_revealed: bool = True
    stage: str = "lobby"
    message: str = "Create or join a room."
    winners: list[dict[str, Any]] = field(default_factory=list)
    straddle_count: int = 0
    last_aggressor_id: str | None = None
    river_aggressor_id: str | None = None
    showdown_order: list[str] = field(default_factory=list)
    showdown_index: int = 0
    hand_id: str = ""
    hand_started_at: str = ""
    action_log: list[dict[str, Any]] = field(default_factory=list)
    last_action_started_at: float | None = None
    previous_hands: list[dict[str, Any]] = field(default_factory=list)
    hand_start_stacks: dict[str, dict[str, Any]] = field(default_factory=dict)
    optional_show_player_id: str | None = None
    optional_show_board_count: int = 0
    pending_result_logged: bool = False
    squid_total: int = 0
    squid_price: int = 0
    squid_claimed: int = 0
    squid_active: bool = False
    squid_pending_player_id: str | None = None
    squid_settlements: list[dict[str, Any]] = field(default_factory=list)
    community_boards: list[list[str]] = field(default_factory=list)
    board_choice_order: list[str] = field(default_factory=list)
    board_choice_index: int = 0
    board_choices: dict[str, int] = field(default_factory=dict)
    board_choice_max: int = 1
    board_base_count: int = 0
    run_board_count: int = 1
    chat_messages: list[dict[str, Any]] = field(default_factory=list)

    def ordered_players(self) -> list[Player]:
        return list(self.players.values())

    def seated_players(self) -> list[Player]:
        return [player for player in self.ordered_players() if not player.is_spectator]

    def seated_count(self) -> int:
        return len(self.seated_players())

    def player_role(self, player: Player) -> str:
        if player.is_spectator:
            return "Spectator"
        seated = self.seated_players()
        if player not in seated:
            return "Online"
        index = seated.index(player)
        if seated and index == self.dealer_index:
            return "Dealer"
        if self.stage != "lobby" and len(seated) > 1:
            if index == (self.dealer_index + 1) % len(seated):
                return "Small Blind"
            if index == ((self.dealer_index + 2) % len(seated) if len(seated) > 2 else self.dealer_index):
                return "Big Blind"
        return "Online"

    def active_players(self) -> list[Player]:
        return [player for player in self.seated_players() if player.cards and not player.folded]

    def visible_community(self) -> list[str]:
        if self.stage == "optional_show":
            return self.community_cards[: self.optional_show_board_count]
        if self.community_boards and self.stage in {"reveal", "showdown"}:
            return self.community_boards[0]
        count = {"lobby": 0, "straddle": 0, "preflop": 0, "flop": 3, "turn": 4, "river": 5, "board_choice": len(self.community_cards[:5]) if self.community_boards else len(self.community_cards[: self.board_base_count]), "reveal": 5, "showdown": 5}.get(self.stage, 0)
        return self.community_cards[:count]

    def current_time_left(self) -> int:
        if self.action_deadline is None or self.stage not in {"straddle", *BETTING_STAGES}:
            return 0
        return max(0, int(self.action_deadline - time.time()))

    def start_action_clock(self) -> None:
        now = time.time()
        self.action_deadline = now + max(5, self.action_timeout)
        self.last_action_started_at = now

    def begin_hand_history(self) -> None:
        stamp = datetime.now()
        room_slug = history_slug(self.name or self.code)
        self.hand_id = f"{stamp.strftime('%Y%m%d_%H%M%S')}_{room_slug}_{self.code}"
        self.hand_started_at = stamp.isoformat(timespec="seconds")
        self.action_log = []
        self.hand_start_stacks = {player.id: {"name": player.name, "before": player.chips} for player in self.seated_players()}
        self.last_action_started_at = None

    def action_thinking_time(self) -> float:
        if self.last_action_started_at is None:
            return 0.0
        return round(max(0.0, time.time() - self.last_action_started_at), 1)

    def append_history_entry(self, entry: dict[str, Any], persist: bool = True) -> None:
        self.action_log.append(entry)
        if persist:
            self.write_hand_history(completed=self.stage == "showdown")

    def log_action(self, player: Player, action: str, amount: int = 0, note: str = "", thinking_time: float | None = None, stack_before: int | None = None, stack_after: int | None = None, cards: list[str] | None = None) -> None:
        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "stage": self.stage,
            "playerId": player.id,
            "player": player.name,
            "action": action,
            "amount": amount,
            "pot": self.pot,
            "thinkingTime": self.action_thinking_time() if thinking_time is None else thinking_time,
            "note": note,
            "stackBefore": stack_before,
            "stackAfter": stack_after,
        }
        if cards is not None:
            entry["cards"] = cards
        self.append_history_entry(entry)

    def log_system_action(self, action: str, note: str = "", cards: list[str] | None = None) -> None:
        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "stage": self.stage,
            "playerId": "",
            "player": "System",
            "action": action,
            "amount": 0,
            "pot": self.pot,
            "thinkingTime": 0.0,
            "note": note,
            "stackBefore": None,
            "stackAfter": None,
        }
        if cards is not None:
            entry["cards"] = cards
        self.append_history_entry(entry)

    def log_board_deal(self) -> None:
        if self.stage == "flop":
            cards = self.community_cards[:3]
            self.log_system_action("deal flop", note=" ".join(cards), cards=cards)
        elif self.stage == "turn":
            cards = self.community_cards[3:4]
            self.log_system_action("deal turn", note=" ".join(cards), cards=cards)
        elif self.stage == "river":
            cards = self.community_cards[4:5]
            self.log_system_action("deal river", note=" ".join(cards), cards=cards)

    def peek_summary(self) -> dict[str, Any]:
        actions = [entry for entry in self.action_log if entry.get("action") == "wanna see see"]
        totals: dict[str, dict[str, Any]] = {}
        rows = []
        for entry in actions:
            name = entry.get("player") or "Player"
            amount = int(entry.get("amount") or 0)
            item = totals.setdefault(name, {"name": name, "count": 0, "spent": 0})
            item["count"] += 1
            item["spent"] += amount
            rows.append({"time": entry.get("time", ""), "player": name, "amount": amount, "note": entry.get("note", "")})
        return {"count": len(actions), "total": sum(int(entry.get("amount") or 0) for entry in actions), "byPlayer": list(totals.values()), "actions": rows[-30:]}

    def scoreboard_rows(self) -> list[dict[str, Any]]:
        rows = []
        played_ids = {entry.get("playerId") for entry in self.action_log if entry.get("playerId")}
        played_names = {entry.get("player") for entry in self.action_log if entry.get("player") not in {None, "", "System", "Result"}}
        for hand in self.previous_hands:
            for stack in hand.get("stacks", []):
                if stack.get("playerId"):
                    played_ids.add(stack.get("playerId"))
                if stack.get("name"):
                    played_names.add(stack.get("name"))
        for player in self.ordered_players():
            if not (player.has_played or player.id in played_ids or player.name in played_names or not player.is_spectator or player.rebuy_units != 1 or player.pending_rebuy_units):
                continue
            effective_units = player.rebuy_units + (player.pending_rebuy_units if player.refill_submitted else 0)
            buy_in = effective_units * STARTING_CHIPS
            rows.append({"id": player.id, "name": player.name, "chips": player.chips, "buyIns": effective_units, "buyInChips": buy_in, "net": player.chips - buy_in, "isPokerGod": False})
        rows.append({"id": "poker-god", "name": "Poker God", "chips": self.poker_god_chips, "buyIns": 0, "buyInChips": 0, "net": self.poker_god_chips, "isPokerGod": True})
        return rows

    def add_chat_message(self, player_id: str, text: str) -> str | None:
        player = self.players[player_id]
        clean = " ".join(str(text or "").strip().split())[:180]
        if not clean:
            return "Chat message is empty."
        self.chat_messages.append({"time": datetime.now().isoformat(timespec="seconds"), "playerId": player.id, "player": player.name, "text": clean})
        self.chat_messages = self.chat_messages[-80:]
        return None
    def log_result(self) -> None:
        if not self.winners:
            return
        summary = "; ".join(f"{winner['name']} wins {winner['amount']} with {winner['hand']}" for winner in self.winners if winner.get("amount", 0) > 0)
        self.append_history_entry({
            "time": datetime.now().isoformat(timespec="seconds"),
            "stage": "result",
            "playerId": "",
            "player": "Result",
            "action": summary or "No winner",
            "amount": 0,
            "pot": self.pot,
            "thinkingTime": 0.0,
            "note": "hand complete",
            "stackBefore": None,
            "stackAfter": None,
        }, persist=False)

    def hand_stack_summary(self) -> list[dict[str, Any]]:
        summaries = []
        current_players = {player.id: player for player in self.seated_players()}
        ordered_ids = list(self.hand_start_stacks.keys())
        for player_id in current_players:
            if player_id not in self.hand_start_stacks:
                ordered_ids.append(player_id)
        for player_id in ordered_ids:
            start = self.hand_start_stacks.get(player_id, {})
            player = current_players.get(player_id)
            name = (player.name if player is not None else start.get("name")) or "Player"
            before = start.get("before", player.chips if player is not None else 0)
            after = player.chips if player is not None else start.get("after", before)
            summaries.append({"playerId": player_id, "name": name, "before": before, "after": after})
        return summaries

    def board_by_street(self, completed: bool = False) -> dict[str, list[str]]:
        visible_count = 5 if completed else len(self.visible_community())
        return {
            "flop": self.community_cards[:3] if visible_count >= 3 else [],
            "turn": self.community_cards[3:4] if visible_count >= 4 else [],
            "river": self.community_cards[4:5] if visible_count >= 5 else [],
        }

    def player_hand_summary(self, include_cards: bool = False) -> list[dict[str, Any]]:
        players = []
        for player in self.seated_players():
            item = {
                "playerId": player.id,
                "name": player.name,
                "folded": player.folded,
                "cardsVisible": player.cards_visible,
                "finalStack": player.chips,
            }
            if include_cards:
                item["cards"] = player.cards
            else:
                item["cardCount"] = len(player.cards)
            players.append(item)
        return players

    def hand_history_payload(self, completed: bool = False) -> dict[str, Any]:
        board = self.board_by_street(completed)
        return {
            "roomCode": self.code,
            "roomName": self.name or self.code,
            "handId": self.hand_id,
            "status": "completed" if completed else "in_progress",
            "startedAt": self.hand_started_at,
            "endedAt": datetime.now().isoformat(timespec="seconds") if completed else None,
            "smallBlind": self.small_blind,
            "bigBlind": self.big_blind,
            "communityCards": board["flop"] + board["turn"] + board["river"],
            "board": board,
            "boards": self.community_boards if self.community_boards else [],
            "winners": self.winners,
            "stacks": self.hand_stack_summary(),
            "players": self.player_hand_summary(include_cards=completed),
            "actions": self.action_log,
            "peekSummary": self.peek_summary(),
            "scoreboard": self.scoreboard_rows(),
        }

    def write_hand_history(self, completed: bool = False) -> dict[str, Any] | None:
        if not self.hand_id:
            return None
        folder = HISTORY_ROOT / self.hand_id
        folder.mkdir(parents=True, exist_ok=True)
        payload = self.hand_history_payload(completed)
        (folder / "betting_history.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    def save_hand_history(self) -> None:
        payload = self.write_hand_history(completed=True)
        if payload is None:
            return
        summary = {
            "handId": self.hand_id,
            "endedAt": payload["endedAt"],
            "winners": self.winners,
            "stacks": payload["stacks"],
            "path": str(HISTORY_ROOT / self.hand_id / "betting_history.json"),
        }
        self.previous_hands = [hand for hand in self.previous_hands if hand.get("handId") != self.hand_id]
        self.previous_hands.append(summary)
        self.previous_hands = self.previous_hands[-20:]

    def public_state(self) -> dict[str, Any]:
        players = self.ordered_players()
        seated = self.seated_players()
        live_bankroll = sum(player.chips for player in seated) + self.pot + self.poker_god_chips
        peek_summary = self.peek_summary()
        scoreboard = self.scoreboard_rows()
        buy_in_bankroll = sum(player.rebuy_units for player in seated) * STARTING_CHIPS
        effective_buy_in_bankroll = sum((player.rebuy_units + (player.pending_rebuy_units if player.refill_submitted else 0)) for player in seated) * STARTING_CHIPS
        return {
            "roomCode": self.code,
            "roomName": self.name or self.code,
            "stage": self.stage,
            "pot": self.pot,
            "currentBet": self.current_bet,
            "minRaise": self.min_raise,
            "smallBlind": self.small_blind,
            "bigBlind": self.big_blind,
            "actionTimeout": self.action_timeout,
            "buyTimeSeconds": self.buy_time_seconds,
            "peekPriceMin": self.peek_price_min,
            "peekPriceMax": self.peek_price_max,
            "buyTimePrice": self.big_blind * 2,
            "pokerGodChips": self.poker_god_chips,
            "peekSummary": peek_summary,
            "scoreboard": scoreboard,
            "chatMessages": self.chat_messages[-40:],
            "bankrollAudit": {"live": live_bankroll, "buyIns": buy_in_bankroll, "effectiveBuyIns": effective_buy_in_bankroll},
            "timeLeft": self.current_time_left(),
            "actionDeadline": self.action_deadline,
            "actionStartedAt": self.last_action_started_at,
            "cardsRevealed": self.cards_revealed,
            "straddleAmount": self.straddle_amount,
            "dealerIndex": self.dealer_index,
            "turnIndex": self.turn_index,
            "currentPlayerId": self.optional_show_player_id if self.stage == "optional_show" else (self.current_board_choice_player_id() if self.stage == "board_choice" else (self.current_showdown_player_id() if self.stage == "reveal" else (seated[self.straddle_index].id if self.stage == "straddle" and seated else (seated[self.turn_index].id if self.stage in BETTING_STAGES and seated else None)))),
            "communityCards": self.visible_community(),
            "community": self.visible_community(),
            "communityBoards": self.community_boards if self.community_boards and self.stage in {"reveal", "showdown"} else [],
            "boardChoice": {"active": self.stage == "board_choice", "max": self.board_choice_max, "choices": {pid: count for pid, count in self.board_choices.items()}, "currentPlayerId": self.current_board_choice_player_id(), "count": self.run_board_count},
            "message": self.message,
            "winners": self.winners,
            "handHistory": self.action_log[-80:],
            "previousHands": self.previous_hands[-10:],
            "squid": {
                "active": self.squid_active,
                "total": self.squid_total,
                "price": self.squid_price,
                "claimed": self.squid_claimed,
                "remaining": max(0, self.squid_total - self.squid_claimed),
                "settlements": self.squid_settlements if self.squid_active else [],
                "holders": [{"name": player.name, "squids": player.squids} for player in self.seated_players() if player.squids > 0],
                "pendingPlayer": self.players[self.squid_pending_player_id].name if self.squid_pending_player_id in self.players else "",
                "canConfigure": (not self.squid_active) or self.stage in {"lobby", "showdown"},
                "minimum": self.min_squid_total(),
            },
            "canStartHand": self.can_start_hand(),
            "maxSeats": MAX_SEATS,
            "players": [
                {
                    "id": player.id,
                    "name": player.name,
                    "chips": player.chips,
                    "connected": player.connected,
                    "isSpectator": player.is_spectator,
                    "wantsSeat": player.wants_seat,
                    "cardCount": len(player.cards),
                    "folded": player.folded,
                    "allIn": player.all_in,
                    "currentBet": player.current_bet,
                    "checked": player.checked_marker,
                    "cardsVisible": player.cards_visible,
                    "cards": player.cards if (player.cards_visible and self.stage in {"reveal", "optional_show", "showdown"}) else [],
                    "readyNextHand": player.ready_next_hand,
                    "rebuyUnits": player.rebuy_units,
                    "pendingRebuyUnits": player.pending_rebuy_units,
                    "refillSubmitted": player.refill_submitted,
                    "squids": player.squids,
                    "role": self.player_role(player),
                    "colorIndex": players.index(player) % 10,
                    "isDealer": (not player.is_spectator and seated.index(player) == self.dealer_index) if player in seated else False,
                    "isTurn": ((self.stage in BETTING_STAGES and player in seated and seated.index(player) == self.turn_index) or (self.stage == "straddle" and player in seated and seated.index(player) == self.straddle_index) or (self.stage == "reveal" and self.current_showdown_player_id() == player.id) or (self.stage == "optional_show" and self.optional_show_player_id == player.id)),
                    "canClaimSquid": self.can_player_claim_squid(player),
                    "effectiveRebuyUnits": player.rebuy_units + (player.pending_rebuy_units if player.refill_submitted else 0),
                }
                for player in players
            ],
        }

    def private_state_for(self, player_id: str) -> dict[str, Any]:
        player = self.players[player_id]
        call_amount = max(0, self.current_bet - player.current_bet)
        show_cards = bool(player.cards) and (player.cards_visible or player.folded or (not player.folded and (self.cards_revealed or self.stage in {"reveal", "optional_show", "showdown"})))
        state = self.public_state()
        paid_views = set(self.private_card_views.get(player_id, []))
        for item in state["players"]:
            target = self.players.get(item["id"])
            if target is not None and target.id in paid_views and target.cards:
                item["cards"] = target.cards
                item["privatePeek"] = True
            item["canPeek"] = self.can_player_peek(player_id, item["id"])
        return {
            "type": "state",
            "state": state,
            "you": {
                "id": player.id,
                "name": player.name,
                "chips": player.chips,
                "currentBet": player.current_bet,
                "folded": player.folded,
                "cards": player.cards if show_cards else [],
                "isSpectator": player.is_spectator,
                "wantsSeat": player.wants_seat,
                "canRequestSeat": player.is_spectator and not self.squid_active,
                "canAct": self.can_player_act(player_id),
                "canBuyTime": self.can_player_buy_time(player_id),
                "canStraddle": self.can_player_straddle(player_id),
                "canShowdownAct": self.can_player_showdown_act(player_id),
                "canOptionalShow": self.can_player_optional_show(player_id),
                "canChooseBoards": self.can_player_choose_boards(player_id),
                "boardChoiceMax": self.board_choice_max,
                "canShowEndedHand": self.can_show_ended_hand(player_id),
                "callAmount": min(call_amount, player.chips),
                "minRaiseTo": self.current_bet + self.min_raise,
                "readyNextHand": player.ready_next_hand,
                "rebuyUnits": player.rebuy_units,
                "effectiveRebuyUnits": player.rebuy_units + (player.pending_rebuy_units if player.refill_submitted else 0),
                "pendingRebuyUnits": player.pending_rebuy_units,
                "refillSubmitted": player.refill_submitted,
                "canReady": self.stage in {"lobby", "showdown"} and not player.is_spectator,
                "canShuffleSeats": self.stage in {"lobby", "showdown"} and not player.is_spectator,
            },
        }

    def pending_ready_names(self) -> list[str]:
        return [
            player.name
            for player in self.seated_players()
            if player.connected and not player.ready_next_hand
        ]

    def pending_ready_message(self) -> str:
        pending = self.pending_ready_names()
        if not pending:
            return "All seated players are ready. Start the next hand."
        return "Waiting for ready: " + ", ".join(pending) + "."

    def can_player_act(self, player_id: str) -> bool:
        seated = self.seated_players()
        return self.stage in BETTING_STAGES and bool(seated) and seated[self.turn_index].id == player_id and self.players[player_id].can_act()

    def can_player_straddle(self, player_id: str) -> bool:
        seated = self.seated_players()
        return self.stage == "straddle" and bool(seated) and seated[self.straddle_index].id == player_id and self.players[player_id].can_act()

    def current_actor_id(self) -> str | None:
        seated = self.seated_players()
        if not seated:
            return None
        if self.stage == "straddle":
            return seated[self.straddle_index].id
        if self.stage in BETTING_STAGES:
            return seated[self.turn_index].id
        return None

    def current_showdown_player_id(self) -> str | None:
        if self.stage != "reveal" or self.showdown_index >= len(self.showdown_order):
            return None
        return self.showdown_order[self.showdown_index]

    def can_player_showdown_act(self, player_id: str) -> bool:
        return self.current_showdown_player_id() == player_id

    def current_board_choice_player_id(self) -> str | None:
        if self.stage != "board_choice" or self.board_choice_index >= len(self.board_choice_order):
            return None
        return self.board_choice_order[self.board_choice_index]

    def can_player_choose_boards(self, player_id: str) -> bool:
        return self.current_board_choice_player_id() == player_id

    def board_choice_candidates(self) -> list[Player]:
        contenders = [player for player in self.active_players() if not player.folded]
        order = [pid for pid in self.movement_order_ids() if any(player.id == pid for player in contenders)]
        return [self.players[pid] for pid in order]

    def max_run_board_count(self, base_count: int) -> int:
        missing = max(0, 5 - base_count)
        if missing == 0:
            return 1
        return max(1, min(4, 1 + len(self.deck) // missing))

    def maybe_start_board_choice(self) -> bool:
        contenders = [player for player in self.active_players() if not player.folded]
        if self.stage not in BETTING_STAGES or len(contenders) < 2 or self.community_boards:
            return False
        if not any(player.all_in for player in contenders):
            return False
        if len([player for player in contenders if player.can_act()]) > 1:
            return False
        base_count = len(self.visible_community())
        if base_count >= 5:
            return False
        candidates = self.board_choice_candidates()
        if len(candidates) < 2:
            return False
        self.stage = "board_choice"
        self.action_deadline = None
        self.board_base_count = base_count
        self.board_choice_max = self.max_run_board_count(base_count)
        self.board_choice_order = [player.id for player in candidates]
        self.board_choice_index = 0
        self.board_choices = {}
        self.run_board_count = 1
        current = candidates[0]
        self.message = f"All-in locked. {current.name} choose how many boards to run (1-{self.board_choice_max}). Lowest choice decides."
        return True

    def build_run_boards(self, count: int) -> None:
        base = self.community_cards[: self.board_base_count]
        first_board = self.community_cards[:5]
        boards = [first_board]
        for _ in range(1, count):
            board = list(base)
            while len(board) < 5 and self.deck:
                board.append(self.deck.pop())
            if len(board) == 5:
                boards.append(board)
        self.community_boards = boards
        self.run_board_count = len(boards)
        self.log_system_action(f"deal {self.run_board_count} boards", cards=[card for board in boards for card in board])

    def perform_board_choice(self, player_id: str, count: int) -> str | None:
        if not self.can_player_choose_boards(player_id):
            return "It is not your board choice."
        choice = max(1, min(self.board_choice_max, int(count or 1)))
        player = self.players[player_id]
        self.board_choices[player_id] = choice
        self.log_action(player, "choose boards", choice, thinking_time=0.0)
        self.board_choice_index += 1
        if self.board_choice_index < len(self.board_choice_order):
            current = self.players[self.board_choice_order[self.board_choice_index]]
            self.message = f"{player.name} chose {choice}. {current.name} choose how many boards to run (1-{self.board_choice_max}). Lowest choice decides."
            return None
        final_count = min(self.board_choices.values()) if self.board_choices else 1
        self.build_run_boards(final_count)
        self.message = f"Running {self.run_board_count} board{'s' if self.run_board_count != 1 else ''}. Showdown reveal."
        self.start_showdown_reveal()
        return None
    def can_player_optional_show(self, player_id: str) -> bool:
        return self.stage == "optional_show" and self.optional_show_player_id == player_id

    def min_squid_total(self) -> int:
        return len([player for player in self.seated_players() if player.connected])

    def configure_squid(self, total: int, price: int) -> str | None:
        if self.squid_active and self.stage not in {"lobby", "showdown"}:
            return "Active squid settings can be changed only between hands."
        if total < 0 or price < 0:
            return "Squid count and price cannot be negative."
        min_total = self.min_squid_total()
        if total > 0 and total < min_total:
            return f"Squid count must be at least the current seated player count ({min_total})."
        if self.squid_active and total > 0 and total < self.squid_claimed:
            return f"Squid count cannot be below already claimed squids ({self.squid_claimed})."
        if total == 0 or price == 0:
            self.squid_total = 0
            self.squid_price = 0
            self.squid_claimed = 0
            self.squid_active = False
            self.squid_pending_player_id = None
            self.squid_settlements = []
            for player in self.seated_players():
                player.squids = 0
            self.message = "Squid game cleared."
            return None
        if self.squid_active:
            self.squid_total = total
            self.squid_price = price
            self.message = f"Squid game adjusted: {total} squids at {price} each."
            return None
        self.squid_total = total
        self.squid_price = price
        self.squid_claimed = 0
        self.squid_active = True
        self.squid_pending_player_id = None
        self.squid_settlements = []
        for player in self.seated_players():
            player.squids = 0
        self.message = f"New squid game set: {total} squids at {price} each."
        return None

    def earliest_without_squid_from_big_blind(self, without: list[Player]) -> Player | None:
        seated = self.seated_players()
        if not seated or not without:
            return None
        without_ids = {player.id for player in without}
        big_blind_index = ((self.dealer_index + 2) % len(seated)) if len(seated) > 2 else self.dealer_index
        for offset in range(len(seated)):
            candidate = seated[(big_blind_index + offset) % len(seated)]
            if candidate.id in without_ids:
                return candidate
        return without[0]

    def start_next_squid_series(self, starter: Player | None, total: int, price: int) -> str:
        self.squid_total = total
        self.squid_price = price
        self.squid_claimed = 0
        self.squid_active = total > 0 and price > 0
        self.squid_pending_player_id = starter.id if self.squid_active and starter is not None else None
        self.squid_settlements = []
        for player in self.seated_players():
            player.squids = 0
        if not self.squid_active or starter is None:
            return ""
        self.log_action(starter, "squid target", 0, note="must win and show to start next series", thinking_time=0.0)
        return f"\nNext squid series waiting for {starter.name}. Only {starter.name} can claim the first squid by winning and showing."

    def can_player_claim_squid(self, player: Player) -> bool:
        if not self.squid_active or self.squid_claimed >= self.squid_total:
            return False
        return not self.squid_pending_player_id or player.id == self.squid_pending_player_id

    def maybe_award_squid(self, player: Player) -> str:
        if not self.squid_active or self.squid_claimed >= self.squid_total:
            return ""
        if self.squid_pending_player_id and player.id != self.squid_pending_player_id:
            target = self.players.get(self.squid_pending_player_id)
            target_name = target.name if target else "the reserved player"
            return f"\nSquid is waiting for {target_name}. {player.name} cannot claim it yet."
        player.squids += 1
        self.squid_claimed += 1
        self.squid_pending_player_id = None
        self.log_action(player, "squid", 1, note=f"claimed {self.squid_claimed}/{self.squid_total}", thinking_time=0.0)
        note = f"\n{player.name} claimed a squid ({self.squid_claimed}/{self.squid_total})."
        end_note = self.check_squid_end()
        return note + end_note

    def check_squid_end(self) -> str:
        if not self.squid_active:
            return ""
        seated = self.seated_players()
        without = [player for player in seated if player.squids == 0]
        if self.squid_claimed < self.squid_total and len(without) > 1:
            return ""
        holders = [player for player in seated if player.squids > 0]
        settlements: list[dict[str, Any]] = []
        for payer in without:
            for holder in holders:
                amount = holder.squids * self.squid_price
                if amount <= 0:
                    continue
                payer_before = payer.chips
                holder_before = holder.chips
                payer.chips -= amount
                holder.chips += amount
                settlements.append({
                    "from": payer.name,
                    "to": holder.name,
                    "amount": amount,
                    "fromBefore": payer_before,
                    "fromAfter": payer.chips,
                    "toBefore": holder_before,
                    "toAfter": holder.chips,
                })
                self.log_action(payer, "squid pay", amount, note=f"to {holder.name}", thinking_time=0.0, stack_before=payer_before, stack_after=payer.chips)
                self.log_action(holder, "squid receive", amount, note=f"from {payer.name}", thinking_time=0.0, stack_before=holder_before, stack_after=holder.chips)
        self.squid_settlements = settlements
        next_total = self.squid_total
        next_price = self.squid_price
        auto_starter = self.earliest_without_squid_from_big_blind(without)
        self.squid_active = False
        self.squid_total = 0
        self.squid_claimed = 0
        self.squid_price = 0
        self.squid_pending_player_id = None
        if settlements:
            summary = "\n".join(f"{item['from']} pays {item['to']} {item['amount']}" for item in settlements)
            note = "\nSquid game ended.\n" + summary + "\nSquids reset to 0."
        else:
            note = "\nSquid game ended with no payments.\nSquids reset to 0."
        note += self.start_next_squid_series(auto_starter, next_total, next_price)
        return note

    def live_hand_in_progress(self) -> bool:
        return self.stage in {"straddle", "preflop", "flop", "turn", "river", "board_choice", "reveal", "optional_show"}

    def is_deuce_seven_offsuit(self, player: Player) -> bool:
        if len(player.cards) != 2:
            return False
        ranks = {rank_of(card) for card in player.cards}
        suits = {suit_of(card) for card in player.cards}
        return ranks == {"2", "7"} and len(suits) == 2

    def award_deuce_seven_bounty(self, winners: list[Player]) -> str:
        bounty_winners = [winner for winner in winners if self.is_deuce_seven_offsuit(winner)]
        if not bounty_winners:
            return ""
        notes: list[str] = []
        bounty = self.big_blind * 5
        seated = self.seated_players()
        for winner in bounty_winners:
            total = 0
            for payer in seated:
                if payer.id == winner.id:
                    continue
                payer_before = payer.chips
                winner_before = winner.chips
                payer.chips -= bounty
                winner.chips += bounty
                total += bounty
                self.log_action(payer, "27o pay", bounty, note=f"to {winner.name}", thinking_time=0.0, stack_before=payer_before, stack_after=payer.chips)
                self.log_action(winner, "27o receive", bounty, note=f"from {payer.name}", thinking_time=0.0, stack_before=winner_before, stack_after=winner.chips)
            if total:
                notes.append(f"{winner.name} wins with 27 offsuit. Everyone pays {bounty}; {winner.name} receives {total}.")
        return ("\n" + "\n".join(notes)) if notes else ""

    def can_show_ended_hand(self, player_id: str) -> bool:
        player = self.players.get(player_id)
        return bool(player and self.stage == "showdown" and player.cards and not player.cards_visible and not player.is_spectator)

    def show_ended_hand(self, player_id: str) -> str | None:
        if not self.can_show_ended_hand(player_id):
            return "You cannot show your hand now."
        player = self.players[player_id]
        player.cards_visible = True
        self.log_action(player, "show", 0, note="after hand ended", cards=player.cards)
        self.message = f"{player.name} shows their hand.\n" + self.pending_ready_message()
        self.save_hand_history()
        return None

    def can_player_peek(self, viewer_id: str, target_id: str) -> bool:
        viewer = self.players.get(viewer_id)
        target = self.players.get(target_id)
        if viewer is None or target is None or viewer.id == target.id:
            return False
        if not self.live_hand_in_progress() or not viewer.folded or viewer.is_spectator or not target.cards:
            return False
        if target.id in set(self.private_card_views.get(viewer.id, [])) or target.cards_visible:
            return False
        return viewer.chips >= self.peek_price_min

    def peek_hand(self, viewer_id: str, target_id: str) -> str | None:
        if not self.can_player_peek(viewer_id, target_id):
            return "You can only peek after folding during a live hand."
        viewer = self.players[viewer_id]
        target = self.players[target_id]
        low, high = sorted((max(0, self.peek_price_min), max(0, self.peek_price_max)))
        price = random.randint(low, high)
        if viewer.chips < price:
            return f"Need {price} chips to wanna see see."
        before = viewer.chips
        viewer.chips -= price
        self.poker_god_chips += price
        views = self.private_card_views.setdefault(viewer.id, [])
        if target.id not in views:
            views.append(target.id)
        self.log_action(viewer, "wanna see see", price, note=f"peeked at {target.name}; paid Poker God", stack_before=before, stack_after=viewer.chips)
        self.message = f"{viewer.name} pays Poker God {price} to wanna see see {target.name}."
        return None
    def can_player_buy_time(self, player_id: str) -> bool:
        player = self.players.get(player_id)
        return bool(
            player
            and self.action_deadline is not None
            and time.time() < self.action_deadline
            and self.stage in {"straddle", *BETTING_STAGES}
            and self.current_actor_id() == player_id
            and player.chips >= self.big_blind * 2
        )

    def buy_time(self, player_id: str) -> str | None:
        if not self.can_player_buy_time(player_id):
            return "Buy time is available only to the acting player while the clock is running."
        player = self.players[player_id]
        price = self.big_blind * 2
        before = player.chips
        player.chips -= price
        self.poker_god_chips += price
        self.action_deadline = max(self.action_deadline or time.time(), time.time()) + self.buy_time_seconds
        self.log_action(player, "buy time", price, note=f"+{self.buy_time_seconds}s to Poker God", stack_before=before, stack_after=player.chips)
        self.message = f"{player.name} buys {self.buy_time_seconds}s for {price}. Poker God receives {price}."
        return None
    def configure_blinds(self, small_blind: int, big_blind: int, action_timeout: int | None = None, buy_time_seconds: int | None = None, peek_price_min: int | None = None, peek_price_max: int | None = None) -> str | None:
        if self.stage not in {"lobby", "showdown"}:
            return "Blinds and clock can be changed after the hand ends."
        if small_blind <= 0 or big_blind < small_blind:
            return "Big blind must be at least the small blind."
        self.small_blind = small_blind
        self.big_blind = big_blind
        self.min_raise = big_blind
        if action_timeout is not None:
            self.action_timeout = max(5, action_timeout)
        if buy_time_seconds is not None:
            self.buy_time_seconds = max(1, buy_time_seconds)
        if peek_price_min is not None:
            self.peek_price_min = max(0, peek_price_min)
        if peek_price_max is not None:
            self.peek_price_max = max(self.peek_price_min, peek_price_max)
        timing = "now" if self.stage == "lobby" else "for the next hand"
        self.message = f"Blinds set to {small_blind}/{big_blind} {timing}. Clock: {self.action_timeout}s. One buy-time: {self.buy_time_seconds}s. Peek: {self.peek_price_min}-{self.peek_price_max}."
        return None
    def can_start_hand(self) -> bool:
        seated_with_chips = [player for player in self.seated_players() if player.connected and (player.chips + (player.pending_rebuy_units * STARTING_CHIPS if player.refill_submitted else 0)) > 0]
        if len(seated_with_chips) < 2 or self.stage not in {"lobby", "showdown"}:
            return False
        return all(player.ready_next_hand or not player.connected for player in self.seated_players())

    def request_seat(self, player_id: str) -> str | None:
        player = self.players[player_id]
        if not player.is_spectator:
            return "You are already seated."
        if self.squid_active:
            return "Awaiting players can join after this squid series ends."
        if self.seated_count() >= MAX_SEATS:
            return "Table is full."
        player.wants_seat = True
        player.ready_next_hand = False
        if self.stage in {"lobby", "showdown"}:
            player.is_spectator = False
            player.wants_seat = False
            self.message = f"{player.name} joined the table."
        else:
            self.message = f"{player.name} will join next hand."
        return None

    def mark_ready_next_hand(self, player_id: str) -> str | None:
        if self.stage not in {"lobby", "showdown"}:
            return "Ready is available before the next hand starts."
        player = self.players[player_id]
        if player.is_spectator:
            return "Spectators cannot ready for the next hand."
        player.ready_next_hand = True
        self.message = self.pending_ready_message()
        return None

    def shuffle_seats(self, player_id: str) -> str | None:
        player = self.players[player_id]
        if player.is_spectator:
            return "Spectators cannot shuffle seats."
        if self.stage not in {"lobby", "showdown"}:
            return "Seats can be shuffled only between hands."
        seated = self.seated_players()
        if len(seated) < 2:
            return "Need at least two seated players to shuffle seats."
        dealer_id = seated[self.dealer_index % len(seated)].id if seated else ""
        seated_ids = [item.id for item in seated]
        random.shuffle(seated_ids)
        spectator_ids = [item.id for item in self.ordered_players() if item.is_spectator]
        self.players = {pid: self.players[pid] for pid in [*seated_ids, *spectator_ids] if pid in self.players}
        new_seated = self.seated_players()
        self.dealer_index = next((index for index, item in enumerate(new_seated) if item.id == dealer_id), 0) if new_seated else 0
        self.message = f"{player.name} shuffled the seats for the next hand."
        return None

    def request_refill(self, player_id: str, rebuy_units: int = 0) -> str | None:
        player = self.players[player_id]
        if player.is_spectator:
            return "Spectators cannot request a refill."
        if player.refill_submitted:
            return "Refill already submitted for the next hand."
        chip_change = rebuy_units * STARTING_CHIPS
        if player.chips + chip_change < 0:
            return "Cannot return more refill chips than your current stack."
        if player.rebuy_units + rebuy_units < 0:
            return "Total buy-in cannot be lower than 0."
        player.pending_rebuy_units = rebuy_units
        player.refill_submitted = True
        if rebuy_units == 0:
            self.message = f"{player.name} keeps the same stack for next hand."
        else:
            self.message = f"{player.name} requested {'+' if rebuy_units > 0 else ''}{rebuy_units} refill unit(s) for next hand."
        return None

    def apply_pending_refills(self) -> None:
        for player in self.seated_players():
            if not player.refill_submitted:
                continue
            units = player.pending_rebuy_units
            chip_change = units * STARTING_CHIPS
            if units and player.chips + chip_change >= 0:
                player.chips += chip_change
                player.rebuy_units += units
            player.pending_rebuy_units = 0
            player.refill_submitted = False

    def prepare_next_hand_seats(self) -> None:
        for player in self.seated_players():
            if not player.connected:
                player.is_spectator = True
                player.cards = []
                player.current_bet = 0
                player.total_bet = 0
                player.folded = True
                player.all_in = False
                player.ready_next_hand = False
                player.refill_submitted = False
                player.pending_rebuy_units = 0
        for player in self.ordered_players():
            if (not self.squid_active) and player.is_spectator and player.wants_seat and player.connected and self.seated_count() < MAX_SEATS:
                player.is_spectator = False
                player.wants_seat = False

    def reset_hand(self) -> None:
        if not self.can_start_hand():
            self.message = "Waiting for seated players to click ready."
            return
        self.prepare_next_hand_seats()
        self.apply_pending_refills()
        players = [player for player in self.seated_players() if player.chips > 0 and player.connected]
        if len(players) < 2:
            self.message = "Need at least 2 seated players with chips to start."
            return
        self.dealer_index = (self.dealer_index + 1) % len(players) if self.stage != "lobby" else self.dealer_index % len(players)
        self.deck = make_deck()
        self.community_cards = [self.deck.pop() for _ in range(5)]
        self.pot = 0
        self.current_bet = 0
        self.min_raise = self.big_blind
        self.stage = "straddle"
        self.cards_revealed = False
        self.winners = []
        for player in self.ordered_players():
            player.cards = []
            player.current_bet = 0
            player.total_bet = 0
            player.folded = player.is_spectator or player.chips <= 0 or not player.connected
            player.all_in = player.chips <= 0 and not player.is_spectator
            player.acted = False
            player.ready_next_hand = False
            player.checked_marker = False
            player.cards_visible = False
        for player in players:
            player.has_played = True
            player.cards = [self.deck.pop(), self.deck.pop()]
        small_blind_player = players[(self.dealer_index + 1) % len(players)]
        big_blind_player = players[(self.dealer_index + 2) % len(players)] if len(players) > 2 else players[self.dealer_index]
        self.begin_hand_history()
        self.contribute(small_blind_player, self.small_blind)
        self.contribute(big_blind_player, self.big_blind)
        self.current_bet = max(player.current_bet for player in players)
        first_to_act = (self.dealer_index + 3) % len(players) if len(players) > 2 else (self.dealer_index + 1) % len(players)
        self.straddle_index = self.next_actor_from(first_to_act - 1)
        self.turn_index = self.straddle_index
        self.straddle_amount = self.big_blind * 2
        self.straddle_count = 0
        self.last_straddler_index = None
        self.last_aggressor_id = None
        self.river_aggressor_id = None
        self.showdown_order = []
        self.showdown_index = 0
        self.optional_show_player_id = None
        self.optional_show_board_count = 0
        self.pending_result_logged = False
        self.private_card_views = {}
        self.community_boards = []
        self.board_choice_order = []
        self.board_choice_index = 0
        self.board_choices = {}
        self.board_choice_max = 1
        self.board_base_count = 0
        self.run_board_count = 1
        self.log_action(small_blind_player, "small blind", small_blind_player.current_bet, thinking_time=0.0, stack_before=small_blind_player.chips + small_blind_player.current_bet, stack_after=small_blind_player.chips)
        self.log_action(big_blind_player, "big blind", big_blind_player.current_bet, thinking_time=0.0, stack_before=big_blind_player.chips + big_blind_player.current_bet, stack_after=big_blind_player.chips)
        self.start_action_clock()
        self.message = f"{players[self.straddle_index].name} may straddle to {self.straddle_amount}."

    def contribute(self, player: Player, amount: int) -> int:
        paid = min(max(amount, 0), player.chips)
        player.chips -= paid
        player.current_bet += paid
        player.total_bet += paid
        player.all_in = player.chips == 0 and not player.is_spectator
        self.pot += paid
        return paid

    def finish_straddles(self) -> None:
        self.stage = "preflop"
        self.cards_revealed = True
        self.straddle_amount = 0
        for player in self.seated_players():
            player.acted = not player.can_act()
        start_index = self.last_straddler_index if self.last_straddler_index is not None else self.straddle_index - 1
        self.turn_index = self.next_actor_from(start_index)
        self.start_action_clock()
        seated = self.seated_players()
        self.message = f"Cards are live. {seated[self.turn_index].name}'s turn."

    def perform_straddle(self, player_id: str, straddle: bool) -> str | None:
        if not self.can_player_straddle(player_id):
            return "It is not your straddle option."
        player = self.players[player_id]
        if not straddle:
            self.log_action(player, "no straddle", 0)
            self.finish_straddles()
            return None
        paid = self.contribute(player, max(0, self.straddle_amount - player.current_bet))
        player.cards_visible = True
        player.acted = True
        self.last_straddler_index = self.seated_players().index(player)
        self.current_bet = max(self.current_bet, player.current_bet)
        self.log_action(player, "straddle", player.current_bet, note=f"paid {paid}", stack_before=player.chips + paid, stack_after=player.chips)
        self.min_raise = max(self.big_blind, self.current_bet - self.big_blind)
        self.straddle_amount = max(self.big_blind * 2, self.current_bet * 2)
        self.straddle_count += 1
        self.straddle_index = self.next_actor_from(self.straddle_index)
        if self.straddle_count >= len([p for p in self.seated_players() if p.can_act()]) or self.straddle_index == self.turn_index or not self.seated_players()[self.straddle_index].can_act():
            self.finish_straddles()
            return None
        self.start_action_clock()
        seated = self.seated_players()
        self.message = f"{player.name} straddled to {player.current_bet}. {seated[self.straddle_index].name} may straddle to {self.straddle_amount}."
        return None

    def perform_timeout(self) -> str | None:
        if self.action_deadline is None or time.time() < self.action_deadline:
            return None
        if self.stage == "straddle":
            self.finish_straddles()
            return None
        if self.stage in BETTING_STAGES:
            player = self.seated_players()[self.turn_index]
            player.folded = True
            player.cards_visible = False
            player.checked_marker = False
            player.acted = True
            self.log_action(player, "timeout fold", 0)
            self.message = f"{player.name} timed out and folded."
            self.after_action()
        return None

    def perform_action(self, player_id: str, action: str, amount: int = 0) -> str | None:
        if not self.can_player_act(player_id):
            return "It is not your turn."
        player = self.players[player_id]
        call_amount = max(0, self.current_bet - player.current_bet)
        if action == "fold":
            player.folded = True
            player.cards_visible = False
            player.checked_marker = False
            player.acted = True
            self.log_action(player, "fold", 0)
            self.message = f"{player.name} folded."
        elif action == "check":
            if call_amount > 0:
                return f"Call {call_amount} or fold."
            player.acted = True
            player.checked_marker = True
            self.log_action(player, "check", 0)
            self.message = f"{player.name} checked."
        elif action == "call":
            paid = self.contribute(player, call_amount)
            player.checked_marker = False
            player.acted = True
            self.log_action(player, "call", paid, stack_before=player.chips + paid, stack_after=player.chips)
            self.message = f"{player.name} called {paid}."
        elif action == "all_in":
            paid = self.contribute(player, player.chips)
            player.checked_marker = False
            player.acted = True
            self.log_action(player, "all-in", paid, stack_before=player.chips + paid, stack_after=player.chips)
            if player.current_bet > self.current_bet:
                old_bet = self.current_bet
                self.current_bet = player.current_bet
                if self.current_bet - old_bet >= self.min_raise:
                    self.last_aggressor_id = player.id
                    if self.stage == "river":
                        self.river_aggressor_id = player.id
                    self.min_raise = self.current_bet - old_bet
                    for other in self.seated_players():
                        other.acted = other.id == player.id or not other.can_act()
            self.message = f"{player.name} is all-in for {paid}."
        elif action == "raise":
            min_raise_to = self.current_bet + self.min_raise
            if amount < min_raise_to:
                return f"Raise must be at least {min_raise_to} total."
            raise_to = amount
            extra = raise_to - player.current_bet
            if extra <= call_amount:
                return f"Raise must be at least {min_raise_to} total."
            old_bet = self.current_bet
            paid = self.contribute(player, extra)
            player.checked_marker = False
            if player.current_bet > old_bet:
                self.last_aggressor_id = player.id
                if self.stage == "river":
                    self.river_aggressor_id = player.id
                self.min_raise = max(self.big_blind, player.current_bet - old_bet)
                self.current_bet = player.current_bet
                for other in self.seated_players():
                    other.acted = other.id == player.id or not other.can_act()
                self.log_action(player, "raise", player.current_bet, stack_before=player.chips + paid, stack_after=player.chips)
                self.message = f"{player.name} raised to {player.current_bet}."
            else:
                player.acted = True
                self.log_action(player, "call all-in", paid, stack_before=player.chips + paid, stack_after=player.chips)
                self.message = f"{player.name} called all-in for {paid}."
        else:
            return "Unknown action."
        self.after_action()
        return None

    def after_action(self) -> None:
        live_players = [player for player in self.active_players() if not player.folded]
        if len(live_players) == 1:
            self.award_uncontested(live_players[0])
            return
        if self.round_complete():
            if self.maybe_start_board_choice():
                return
            self.advance_stage()
            return
        self.turn_index = self.next_actor_from(self.turn_index)
        self.start_action_clock()
        seated = self.seated_players()
        self.message = f"{seated[self.turn_index].name}'s turn."

    def round_complete(self) -> bool:
        active = [player for player in self.active_players() if not player.folded]
        actors = [player for player in active if player.can_act()]
        if len(active) <= 1 or not actors:
            return True
        return all(player.acted and player.current_bet == self.current_bet for player in actors)

    def next_actor_from(self, start_index: int) -> int:
        seated = self.seated_players()
        for offset in range(1, len(seated) + 1):
            index = (start_index + offset) % len(seated)
            if seated[index].can_act():
                return index
        return start_index % len(seated)

    def advance_stage(self) -> None:
        for player in self.seated_players():
            player.current_bet = 0
            player.checked_marker = False
            player.acted = not player.can_act()
        self.current_bet = 0
        self.min_raise = self.big_blind
        if self.stage == "preflop":
            self.stage = "flop"
        elif self.stage == "flop":
            self.stage = "turn"
        elif self.stage == "turn":
            self.stage = "river"
            self.river_aggressor_id = None
        else:
            self.start_showdown_reveal()
            return
        self.log_board_deal()
        self.turn_index = self.next_actor_from(self.dealer_index)
        if self.round_complete():
            if self.maybe_start_board_choice():
                return
            self.advance_stage()
            return
        self.start_action_clock()
        seated = self.seated_players()
        self.message = f"{self.stage.title()} dealt. {seated[self.turn_index].name}'s turn."

    def award_uncontested(self, winner: Player) -> None:
        amount = self.pot
        board_count = len(self.visible_community())
        winner.chips += amount
        self.winners = [{"id": winner.id, "name": winner.name, "amount": amount, "hand": "Uncontested"}]
        self.pot = 0
        self.action_deadline = None
        for player in self.seated_players():
            player.ready_next_hand = False
        winner.cards_visible = False
        self.optional_show_player_id = winner.id
        self.optional_show_board_count = board_count
        self.pending_result_logged = False
        self.stage = "optional_show"
        bounty_note = self.award_deuce_seven_bounty([winner])
        self.message = f"{winner.name} wins {amount} uncontested. Show hand to claim a squid?" + bounty_note

    def finish_optional_show(self, player_id: str, show: bool) -> str | None:
        if not self.can_player_optional_show(player_id):
            return "It is not your show decision."
        player = self.players[player_id]
        if show:
            player.cards_visible = True
            self.log_action(player, "show", 0, note="optional reveal")
            self.message = f"{player.name} shows after winning uncontested." + self.maybe_award_squid(player)
        else:
            player.cards_visible = False
            self.log_action(player, "hide", 0, note="optional reveal declined")
            self.message = f"{player.name} keeps the winning hand hidden.\nSquid is not claimed this round."
        self.optional_show_player_id = None
        self.stage = "showdown"
        if not self.pending_result_logged:
            self.log_result()
            self.pending_result_logged = True
        self.message += "\n" + self.pending_ready_message()
        self.save_hand_history()
        return None

    def showdown_start_index(self) -> int:
        seated = self.seated_players()
        return (self.dealer_index + 1) % len(seated) if seated else 0

    def movement_order_ids(self) -> list[str]:
        seated = self.seated_players()
        if not seated:
            return []
        start = self.showdown_start_index()
        return [seated[(start + offset) % len(seated)].id for offset in range(len(seated))]

    def start_showdown_reveal(self) -> None:
        contenders = [player for player in self.active_players() if not player.folded]
        order = [pid for pid in self.movement_order_ids() if any(player.id == pid for player in contenders)]
        if self.river_aggressor_id in order:
            order = [self.river_aggressor_id] + [pid for pid in order if pid != self.river_aggressor_id]
        self.showdown_order = order
        self.showdown_index = 0
        for player in self.seated_players():
            if not player.folded:
                player.cards_visible = False
        self.stage = "reveal"
        self.action_deadline = None
        self.cards_revealed = True
        if not order:
            self.showdown()
            return
        current = self.players[order[0]]
        self.message = f"Showdown: {current.name} must show or muck."

    def perform_showdown_choice(self, player_id: str, show: bool) -> str | None:
        if not self.can_player_showdown_act(player_id):
            return "It is not your showdown decision."
        player = self.players[player_id]
        if show:
            player.cards_visible = True
            self.log_action(player, "show", 0)
            self.message = f"{player.name} shows."
        else:
            player.folded = True
            player.cards_visible = False
            self.log_action(player, "muck", 0)
            self.message = f"{player.name} mucks."
        remaining = [candidate for candidate in self.active_players() if not candidate.folded]
        if len(remaining) == 1:
            winner = remaining[0]
            contribution_levels = sorted({candidate.total_bet for candidate in self.seated_players() if candidate.total_bet > 0})
            previous_level = 0
            amount = 0
            for level in contribution_levels:
                contributors = [candidate for candidate in self.seated_players() if candidate.total_bet >= level]
                side_pot = (level - previous_level) * len(contributors)
                previous_level = level
                if side_pot <= 0:
                    continue
                if len(contributors) == 1:
                    receiver = contributors[0]
                    before = receiver.chips
                    receiver.chips += side_pot
                    self.log_action(receiver, "return", side_pot, note="uncalled side pot", thinking_time=0.0, stack_before=before, stack_after=receiver.chips)
                elif winner.total_bet >= level:
                    winner.chips += side_pot
                    amount += side_pot
            self.winners = [{"id": winner.id, "name": winner.name, "amount": amount, "hand": "Uncontested"}] if amount > 0 else []
            self.pot = 0
            self.stage = "showdown"
            self.message = f"{winner.name} wins {amount}." + (self.maybe_award_squid(winner) if winner.cards_visible else "") + self.award_deuce_seven_bounty([winner])
            for candidate in self.seated_players():
                candidate.ready_next_hand = False
            self.log_result()
            self.message += "\n" + self.pending_ready_message()
            self.save_hand_history()
            return None
        self.showdown_index += 1
        while self.showdown_index < len(self.showdown_order) and self.players[self.showdown_order[self.showdown_index]].folded:
            self.showdown_index += 1
        if self.showdown_index >= len(self.showdown_order):
            self.showdown()
        else:
            current = self.players[self.showdown_order[self.showdown_index]]
            self.message += f"\n{current.name} must show or muck."
        return None

    def showdown(self) -> None:
        contenders = [player for player in self.active_players() if not player.folded and player.cards_visible]
        if not contenders:
            contenders = [player for player in self.active_players() if not player.folded]
            for player in contenders:
                player.cards_visible = True
        boards = self.community_boards if self.community_boards else [self.community_cards]
        board_one_squid_winner: Player | None = None
        if len(boards) > 1 and boards:
            board_one_scores = {player.id: evaluate_best(player.cards + boards[0]) for player in contenders}
            if board_one_scores:
                board_one_best = max(board_one_scores.values())
                board_one_winners = [player for player in contenders if board_one_scores[player.id] == board_one_best]
                if len(board_one_winners) == 1:
                    board_one_squid_winner = board_one_winners[0]
        contribution_levels = sorted({player.total_bet for player in self.seated_players() if player.total_bet > 0})
        previous_level = 0
        awarded: dict[str, dict[str, Any]] = {}
        board_results: list[str] = []
        for level in contribution_levels:
            contributors = [player for player in self.seated_players() if player.total_bet >= level]
            eligible = [player for player in contenders if player.total_bet >= level]
            side_pot = (level - previous_level) * len(contributors)
            previous_level = level
            if side_pot <= 0:
                continue
            if len(contributors) == 1:
                receiver = contributors[0]
                before = receiver.chips
                receiver.chips += side_pot
                self.log_action(receiver, "return", side_pot, note="uncalled side pot", thinking_time=0.0, stack_before=before, stack_after=receiver.chips)
                continue
            if not eligible:
                continue
            board_count = max(1, len(boards))
            base_prize = side_pot // board_count
            board_remainder = side_pot % board_count
            for board_index, board in enumerate(boards):
                board_prize = base_prize + (1 if board_index < board_remainder else 0)
                if board_prize <= 0:
                    continue
                scores = {player.id: evaluate_best(player.cards + board) for player in eligible}
                best_score = max(scores[player.id] for player in eligible)
                winners = [player for player in eligible if scores[player.id] == best_score]
                prize = board_prize // len(winners)
                remainder = board_prize % len(winners)
                winner_names = []
                for index, winner in enumerate(winners):
                    amount = prize + (1 if index < remainder else 0)
                    winner.chips += amount
                    winner_names.append(winner.name)
                    if winner.id not in awarded:
                        hand_label = hand_name(best_score[0]) if board_count == 1 else f"Board {board_index + 1} {hand_name(best_score[0])}"
                        awarded[winner.id] = {"id": winner.id, "name": winner.name, "amount": 0, "hand": hand_label}
                    awarded[winner.id]["amount"] += amount
                if board_count > 1:
                    board_results.append(f"Board {board_index + 1}: {', '.join(winner_names)} wins {board_prize} with {hand_name(best_score[0])}")
        self.winners = [winner for winner in awarded.values() if winner.get("amount", 0) > 0]
        squid_note = ""
        if len(boards) > 1:
            if board_one_squid_winner is not None and board_one_squid_winner.cards_visible:
                squid_note = self.maybe_award_squid(board_one_squid_winner)
        elif len(self.winners) == 1:
            squid_winner = self.players.get(self.winners[0]["id"])
            if squid_winner is not None and squid_winner.cards_visible:
                squid_note = self.maybe_award_squid(squid_winner)
        bounty_note = self.award_deuce_seven_bounty([self.players[winner["id"]] for winner in self.winners if winner["id"] in self.players])
        result_parts = [f"{winner['name']} wins {winner['amount']} with {winner['hand']}" for winner in self.winners]
        board_note = ("\n" + "\n".join(board_results)) if board_results else ""
        self.message = ("; ".join(result_parts) + "." if result_parts else "No chips awarded.") + board_note + squid_note + bounty_note
        self.pot = 0
        self.stage = "showdown"
        self.action_deadline = None
        for player in self.seated_players():
            player.ready_next_hand = False
        self.log_result()
        self.message += "\n" + self.pending_ready_message()
        self.save_hand_history()

def player_to_snapshot(player: Player) -> dict[str, Any]:
    payload = {item.name: getattr(player, item.name) for item in fields(Player)}
    payload["connected"] = False
    return payload


def player_from_snapshot(payload: dict[str, Any]) -> Player:
    allowed = {item.name for item in fields(Player)}
    values = {key: value for key, value in payload.items() if key in allowed}
    values["connected"] = False
    return Player(**values)


def room_to_snapshot(room: Room) -> dict[str, Any]:
    payload = {
        item.name: getattr(room, item.name)
        for item in fields(Room)
        if item.name not in {"players", "sockets"}
    }
    payload["version"] = ROOM_STATE_VERSION
    payload["players"] = {player_id: player_to_snapshot(player) for player_id, player in room.players.items()}
    return payload


def room_from_snapshot(payload: dict[str, Any]) -> Room:
    allowed = {item.name for item in fields(Room)} - {"players", "sockets"}
    values = {key: value for key, value in payload.items() if key in allowed}
    room = Room(**values)
    room.sockets = {}
    room.players = {
        player_id: player_from_snapshot(player_payload)
        for player_id, player_payload in payload.get("players", {}).items()
        if isinstance(player_payload, dict)
    }
    room.message = f"Room {room.code} restored. Players can reconnect."
    return room


class RoomManager:
    def __init__(self) -> None:
        self.rooms: dict[str, Room] = {}
        self.load_rooms()

    def room_state_path(self, code: str) -> Path:
        return ROOM_STATE_ROOT / f"{code.upper()}.json"

    def persist_room(self, room: Room) -> None:
        ROOM_STATE_ROOT.mkdir(parents=True, exist_ok=True)
        path = self.room_state_path(room.code)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(room_to_snapshot(room), indent=2), encoding="utf-8")
        tmp_path.replace(path)

    def load_rooms(self) -> None:
        if not ROOM_STATE_ROOT.exists():
            return
        for path in ROOM_STATE_ROOT.glob("*.json"):
            try:
                room = room_from_snapshot(json.loads(path.read_text(encoding="utf-8-sig")))
            except Exception:
                continue
            self.rooms[room.code.upper()] = room

    def create_room(self, player_name: str, password: str = "", player_password: str = "", room_name: str = "") -> tuple[Room, Player]:
        clean_room_name = room_name.strip()[:32]
        base_code = room_code_from_name(clean_room_name) if clean_room_name else ""
        code = base_code or make_room_code()
        if code in self.rooms:
            suffix = 2
            while f"{code}-{suffix}" in self.rooms:
                suffix += 1
            code = f"{code}-{suffix}"
        clean_room_name = clean_room_name or code
        room = Room(code=code, name=clean_room_name, password_hash=password_hash(password))
        player = Player(id=str(uuid4()), name=player_name, password_hash=password_hash(player_password))
        room.players[player.id] = player
        self.rooms[code] = room
        self.persist_room(room)
        return room, player

    def join_room(self, code: str, player_name: str, player_id: str | None = None, password: str = "", player_password: str = "") -> tuple[Room | None, Player | None, str | None]:
        room = self.rooms.get(code.upper())
        if room is None:
            return None, None, "Room not found"
        normalized_name = player_name.casefold()
        if player_id and player_id in room.players:
            player = room.players[player_id]
            if player.name.casefold() == normalized_name:
                player.name = player_name
                self.persist_room(room)
                return room, player, None
            player_id = None

        if room.password_hash and password_hash(password) != room.password_hash:
            return room, None, "Incorrect room password"

        existing = next((player for player in room.ordered_players() if player.name.casefold() == normalized_name), None)
        if existing is not None:
            duplicate_ids = [
                player.id
                for player in room.ordered_players()
                if player.id != existing.id and player.name.casefold() == normalized_name and player.id not in room.sockets
            ]
            if existing.password_hash and password_hash(player_password) != existing.password_hash:
                return room, None, "Incorrect player password"
            if not existing.password_hash and player_password.strip():
                existing.password_hash = password_hash(player_password)
            for duplicate_id in duplicate_ids:
                room.players.pop(duplicate_id, None)
            existing.name = player_name
            self.persist_room(room)
            return room, existing, None

        as_spectator = room.stage != "lobby" or room.seated_count() >= MAX_SEATS
        player = Player(id=str(uuid4()), name=player_name, password_hash=password_hash(player_password), is_spectator=as_spectator)
        room.players[player.id] = player
        self.persist_room(room)
        return room, player, None

    async def connect(self, room: Room, player: Player, websocket: WebSocket) -> None:
        await websocket.accept()
        player.connected = True
        room.sockets[player.id] = websocket
        self.persist_room(room)
        await self.broadcast_state(room)

    def remove_room(self, room: Room) -> None:
        self.rooms.pop(room.code.upper(), None)
        path = self.room_state_path(room.code)
        if path.exists():
            path.unlink()

    def should_remove_lobby_room(self, room: Room) -> bool:
        return room.stage == "lobby" and not room.sockets

    async def disconnect(self, room: Room, player_id: str) -> None:
        if player_id in room.players:
            room.players[player_id].connected = False
        room.sockets.pop(player_id, None)
        if self.should_remove_lobby_room(room):
            self.remove_room(room)
            return
        self.persist_room(room)
        await self.broadcast_state(room)

    async def broadcast(self, room: Room, payload: dict[str, Any]) -> None:
        dead_player_ids: list[str] = []
        for player_id, socket in room.sockets.items():
            try:
                await socket.send_text(json.dumps(payload))
            except Exception:
                dead_player_ids.append(player_id)
        for player_id in dead_player_ids:
            room.sockets.pop(player_id, None)
            if player_id in room.players:
                room.players[player_id].connected = False

    async def broadcast_state(self, room: Room) -> None:
        dead_player_ids: list[str] = []
        for player_id, socket in room.sockets.items():
            try:
                await socket.send_text(json.dumps(room.private_state_for(player_id)))
            except Exception:
                dead_player_ids.append(player_id)
        for player_id in dead_player_ids:
            room.sockets.pop(player_id, None)
            if player_id in room.players:
                room.players[player_id].connected = False


manager = RoomManager()


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.post("/login")
async def login(payload: dict[str, str]) -> JSONResponse:
    username = payload.get("username", "").strip()
    password = payload.get("password", "")
    invite_code = payload.get("inviteCode", "").strip()
    if not username or not password:
        return JSONResponse({"error": "Enter username and password."}, status_code=400)
    if INVITE_CODE and invite_code.casefold() != INVITE_CODE.strip().casefold():
        return JSONResponse({"error": "Incorrect invite code."}, status_code=403)
    return JSONResponse({"username": username})


@app.get("/history")
async def list_history() -> JSONResponse:
    return JSONResponse({"historyRoot": str(HISTORY_ROOT), "hands": list_saved_histories()})


@app.get("/history/{hand_id}")
async def get_history(hand_id: str) -> JSONResponse:
    path = history_file_path(hand_id)
    if path is None or not path.exists():
        return JSONResponse({"error": "History not found"}, status_code=404)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return JSONResponse({"error": "Could not read history"}, status_code=500)
    return JSONResponse(payload)


@app.get("/history/{hand_id}/download")
async def download_history(hand_id: str):
    path = history_file_path(hand_id)
    if path is None or not path.exists():
        return JSONResponse({"error": "History not found"}, status_code=404)
    return FileResponse(path, media_type="application/json", filename=f"{hand_id}.json")


@app.get("/lobby", response_class=HTMLResponse)
async def lobby_page() -> str:
    return INDEX_HTML


@app.get("/room/{room_code}", response_class=HTMLResponse)
async def room_page(room_code: str) -> str:
    return INDEX_HTML


@app.get("/rooms")
async def list_rooms() -> JSONResponse:
    rooms = []
    for room in manager.rooms.values():
        if manager.should_remove_lobby_room(room):
            continue
        rooms.append({
            "roomCode": room.code,
            "name": room.name or room.code,
            "stage": room.stage,
            "players": len([player for player in room.seated_players() if player.connected]) if room.stage == "lobby" else room.seated_count(),
            "maxSeats": MAX_SEATS,
            "hasPassword": bool(room.password_hash),
        })
    rooms.sort(key=lambda item: item["name"].casefold())
    return JSONResponse({"rooms": rooms})


@app.post("/rooms")
async def create_room(payload: dict[str, str]) -> JSONResponse:
    player_name = payload.get("name", "").strip() or "Player"
    room_password = payload.get("password", "")
    player_password = payload.get("playerPassword", "")
    room_name = payload.get("roomName", "")
    room, player = manager.create_room(player_name, room_password, player_password, room_name)
    return JSONResponse({"roomCode": room.code, "roomName": room.name, "playerId": player.id, "isSpectator": player.is_spectator})


@app.post("/rooms/{room_code}/join")
async def join_room(room_code: str, payload: dict[str, str]) -> JSONResponse:
    player_name = payload.get("name", "").strip() or "Player"
    player_id = payload.get("playerId") or None
    room_password = payload.get("password", "")
    player_password = payload.get("playerPassword", "")
    room, player, error = manager.join_room(room_code, player_name, player_id, room_password, player_password)
    if error:
        status_code = 404 if error == "Room not found" else 403
        return JSONResponse({"error": error}, status_code=status_code)
    return JSONResponse({"roomCode": room.code, "playerId": player.id, "isSpectator": player.is_spectator})


@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML, headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})


@app.websocket("/ws/{room_code}/{player_id}")
async def websocket_endpoint(websocket: WebSocket, room_code: str, player_id: str) -> None:
    room = manager.rooms.get(room_code.upper())
    if room is None or player_id not in room.players:
        await websocket.accept()
        await websocket.send_text(json.dumps({"type": "error", "message": "Room or player not found"}))
        await websocket.close()
        return
    player = room.players[player_id]
    await manager.connect(room, player, websocket)
    try:
        while True:
            raw_message = await websocket.receive_text()
            try:
                message = json.loads(raw_message)
            except json.JSONDecodeError:
                message = {"action": raw_message}
            action = message.get("action")
            error = None
            if action == "start_hand":
                room.reset_hand()
            elif action in {"fold", "check", "call", "raise", "all_in"}:
                try:
                    amount = int(message.get("amount", 0))
                except (TypeError, ValueError):
                    amount = 0
                error = room.perform_action(player_id, action, amount)
            elif action == "choose_boards":
                try:
                    count = int(message.get("count", 1))
                except (TypeError, ValueError):
                    count = 1
                error = room.perform_board_choice(player_id, count)
            elif action in {"straddle", "no_straddle"}:
                error = room.perform_straddle(player_id, action == "straddle")
            elif action in {"show_hand", "muck_hand"}:
                if room.stage == "optional_show":
                    error = room.finish_optional_show(player_id, action == "show_hand")
                else:
                    error = room.perform_showdown_choice(player_id, action == "show_hand")
            elif action == "timeout_fold":
                error = room.perform_timeout()
            elif action == "set_squid":
                try:
                    squid_total = int(message.get("total", 0))
                    squid_price = int(message.get("price", 0))
                except (TypeError, ValueError):
                    squid_total = 0
                    squid_price = 0
                error = room.configure_squid(squid_total, squid_price)
            elif action == "shuffle_seats":
                error = room.shuffle_seats(player_id)
            elif action == "chat":
                error = room.add_chat_message(player_id, message.get("text", ""))
            elif action == "set_blinds":
                try:
                    small_blind = int(message.get("smallBlind", SMALL_BLIND))
                    big_blind = int(message.get("bigBlind", BIG_BLIND))
                    action_timeout = int(message.get("actionTimeout", DEFAULT_ACTION_TIMEOUT))
                    buy_time_seconds = int(message.get("buyTimeSeconds", 30))
                    peek_price_min = int(message.get("peekPriceMin", 2))
                    peek_price_max = int(message.get("peekPriceMax", 10))
                except (TypeError, ValueError):
                    small_blind = SMALL_BLIND
                    big_blind = BIG_BLIND
                    action_timeout = DEFAULT_ACTION_TIMEOUT
                    buy_time_seconds = 30
                    peek_price_min = 2
                    peek_price_max = 10
                error = room.configure_blinds(small_blind, big_blind, action_timeout, buy_time_seconds, peek_price_min, peek_price_max)
            elif action == "buy_time":
                error = room.buy_time(player_id)
            elif action == "show_ended_hand":
                error = room.show_ended_hand(player_id)
            elif action == "peek_hand":
                error = room.peek_hand(player_id, str(message.get("targetId", "")))
            elif action == "ready_next_hand":
                error = room.mark_ready_next_hand(player_id)
            elif action == "rebuy":
                try:
                    units = int(message.get("units", 0))
                except (TypeError, ValueError):
                    units = 0
                error = room.request_refill(player_id, units)
            elif action == "request_seat":
                error = room.request_seat(player_id)
            if error:
                await manager.broadcast(room, {"type": "error", "message": error})
            else:
                manager.persist_room(room)
            await manager.broadcast_state(room)
    except WebSocketDisconnect:
        await manager.disconnect(room, player_id)




INDEX_HTML = """
<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Texas Poker Online</title><style>
:root{color-scheme:dark;--gold:#f5c84b;--line:#344154;--felt:#e78fbd;--felt2:#f6b5d7;--rim:#fff6fb;--wood:#6b3151;--muted:#a8b8ca;--red:#de4c45;--blue:#278ce8;--green:#23965b}*{box-sizing:border-box}body{margin:0;min-height:100vh;overflow-x:hidden;font-family:Inter,"Segoe UI",Arial,sans-serif;background:linear-gradient(rgba(7,10,18,.46),rgba(7,10,18,.58)),url("/pictures/background.png") center/cover fixed no-repeat;color:#f7fbff}.app{min-height:100vh;display:grid;grid-template-columns:clamp(210px,18vw,280px) minmax(0,1fr)}aside{background:#0d121b;border-right:1px solid #263140;padding:clamp(8px,1vw,16px);display:flex;flex-direction:column;gap:10px;min-width:0;overflow:auto}h1{margin:0 0 8px;font-size:24px}input,button{width:100%;border:1px solid #3a4658;border-radius:7px;padding:11px 12px;font:800 15px Inter,"Segoe UI",Arial,sans-serif}input{background:#172131;color:#fff}button{cursor:pointer;background:linear-gradient(#ffd765,#efbd38);color:#090d14;border:0}#join{cursor:pointer;pointer-events:auto}button:disabled{cursor:not-allowed;background:#6f7780;color:#111827;opacity:1}.ghost{background:linear-gradient(#ffd765,#efbd38);color:#090d14}.row{display:grid;grid-template-columns:1fr 62px;gap:8px}.settings{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding-top:10px;border-top:1px solid #2b3544}.wide{grid-column:1/-1}.label{color:var(--muted);font-size:12px;font-weight:900;margin-bottom:-8px}.input-field{display:flex;flex-direction:column;gap:4px;min-width:0}.input-field span{color:#a8b8ca;font-size:11px;font-weight:1000}.input-field input{width:100%}.meta{color:#c9e4ff;font-size:17px;font-weight:1000;line-height:1.45;min-height:60px}.main{position:relative;min-width:0;min-height:100vh;padding:clamp(8px,1.2vw,20px);display:grid;grid-template-columns:minmax(640px,1180px) minmax(260px,360px);grid-template-rows:auto auto auto auto;gap:12px clamp(10px,1.4vw,24px);align-content:start;justify-content:center;overflow:auto}.topbar{grid-column:1/-1;display:flex;justify-content:flex-end;min-height:32px;color:#c5dbf4;font-size:15px;font-weight:900}.poker-god{position:absolute;left:12px;top:12px;z-index:9;border:1px solid #344154;border-radius:8px;background:rgba(10,15,24,.94);padding:8px 12px;color:#ffe28a;font-size:14px;font-weight:1000;line-height:1.35;box-shadow:0 12px 28px #0007}.poker-god div:last-child{color:#f7fbff}.chat-panel{position:absolute;left:126px;top:12px;z-index:9;width:260px;border:1px solid #344154;border-radius:8px;background:rgba(10,15,24,.94);box-shadow:0 12px 28px #0007;overflow:hidden}.chat-title{padding:7px 9px;color:#ffe28a;font-size:13px;font-weight:1000;border-bottom:1px solid #263448}.chat-messages{height:92px;overflow:auto;padding:7px 9px;display:flex;flex-direction:column;gap:5px}.chat-row{font-size:12px;line-height:1.25;color:#d8ecff}.chat-row b{color:#f7fbff}.chat-compose{display:grid;grid-template-columns:1fr 58px;gap:5px;padding:7px;border-top:1px solid #263448}.chat-compose input,.chat-compose button{height:32px;padding:6px 8px;font-size:12px}.buy-time{width:auto;min-width:112px;margin:0 auto 8px;padding:8px 12px;border-radius:999px}.self-hand-row{display:flex;align-items:center;justify-content:center;gap:12px;min-height:94px}.self-hand-row .hand{min-height:94px}.show-hand-side{margin:0;align-self:center;white-space:nowrap}.board-choice-pop{position:absolute;left:126px;top:10px;z-index:12;display:grid;grid-template-columns:repeat(4,34px);gap:5px}.board-choice-pop button{width:34px;height:32px;padding:0;border-radius:999px;font-size:13px}.peek-btn{margin-top:5px;padding:5px 7px;border-radius:999px;font-size:10px;line-height:1;width:auto;min-width:0}.table-shell{grid-column:1;position:relative;width:min(1180px,100%);aspect-ratio:1180/650;height:auto;margin:clamp(4px,1vh,18px) auto 0;min-width:640px}.table{position:absolute;left:53%;top:53%;transform:translate(-50%,-50%);width:82%;height:68%;border-radius:245px;background:radial-gradient(circle at 50% 52%,rgba(255,255,255,.08),transparent 36%),linear-gradient(160deg,color-mix(in srgb,var(--felt2) 80%,transparent),color-mix(in srgb,var(--felt) 80%,transparent));border:14px solid var(--rim);box-shadow:0 0 0 12px var(--wood),0 28px 70px rgba(0,0,0,.55),inset 0 0 80px rgba(0,0,0,.28)}.table:after{content:"🉐Evanston";position:absolute;inset:0;display:grid;place-items:center;font-size:52px;font-weight:1000;color:rgba(255,255,255,.12);pointer-events:none}.board{position:absolute;left:53%;top:36%;transform:translate(-50%,-50%);display:flex;gap:9px;z-index:2}.board.multi{flex-direction:column;gap:4px;align-items:center}.board-row{display:flex;gap:0;align-items:center}.board-row .card{margin-right:-14px}.board-row .card:last-child{margin-right:0}.board-tag{margin-right:8px;color:#ffe28a;font-size:12px;font-weight:1000;text-shadow:0 1px 2px #000;min-width:52px;text-align:right}.pot{position:absolute;left:31%;top:51%;transform:translate(-50%,-50%);z-index:3;text-align:center}.board-choice-hint{position:absolute;left:55%;top:47%;transform:translate(-50%,-50%);z-index:6;width:min(430px,42%);text-align:center;color:#ffffff;font-size:17px;font-weight:1000;text-shadow:0 2px 4px #000;line-height:1.22;pointer-events:none;white-space:normal}.table-notice{margin-top:6px;min-height:20px;color:#cdeaff;font-size:15px;font-weight:1000;text-shadow:0 1px 2px #000;white-space:pre-line;line-height:1.35}.seat-token{position:absolute;width:28px;height:28px;border-radius:50%;display:grid;place-items:center;font-size:11px;font-weight:1000;border:2px solid rgba(255,255,255,.8);box-shadow:0 4px 10px #0009;z-index:8;transform:translate(-50%,-50%)}.seat-token.dealer{background:#f7fbff;color:#111827}.seat-token.sb{background:#8d5cf6;color:#fff}.seat-token.bb{background:#ffd33f;color:#111827}.pot-label,.bet-label,.check-label{display:inline-flex;align-items:center;gap:7px;background:rgba(3,10,15,.9);border:1px solid rgba(255,255,255,.24);border-radius:999px;padding:5px 12px;color:#fff8d7;font-weight:1000;font-size:17px;text-shadow:0 1px 0 #000}.chips{margin-top:6px;display:flex;justify-content:center}.chip{width:19px;height:19px;border-radius:50%;border:2px solid rgba(255,255,255,.6);box-shadow:0 2px 4px #0008}.chip.white{background:#f8fafc}.red{background:#d83d37}.green{background:#63bc5c}.gold{background:#f0aa31}.blue{background:#4c7dea}.clock{margin-top:7px;font-size:16px;font-weight:1000;color:#ffe38a}.seats{position:absolute;inset:0;z-index:4;pointer-events:none}.seat{position:absolute;width:132px;height:104px;transform:translate(-50%,-50%);pointer-events:auto}.seat-cards{position:absolute;display:flex}.seat.self .seat-cards{display:none}.seat.folded .badge,.seat.folded .seat-cards,.hand.folded .card{opacity:.42;filter:grayscale(1)}.seat .card{width:42px;height:58px;margin-right:-10px;border-radius:7px}.badge{position:absolute;top:8px;left:50%;transform:translateX(-50%);min-width:92px;min-height:74px;border:1px solid #ffffff33;border-radius:7px;background:rgba(6,10,17,.92);text-align:center;padding:6px 7px;box-shadow:0 12px 28px #0006}.badge.active{border:1px solid transparent;box-shadow:0 0 26px color-mix(in srgb,var(--timer-color,#22bfff) 78%,transparent),0 0 14px var(--timer-color,#22bfff)}.badge.active:before{content:"";position:absolute;inset:-9px;border-radius:15px;padding:7px;background:conic-gradient(from 0deg,rgba(255,255,255,.2) calc(360deg - var(--timer-deg,360deg)),var(--timer-color,#22bfff) 0);-webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);-webkit-mask-composite:xor;mask-composite:exclude;pointer-events:none;filter:drop-shadow(0 0 10px var(--timer-color,#22bfff))}.badge.active:after{content:"";position:absolute;inset:0;border-radius:7px;background:conic-gradient(from 0deg,transparent calc(360deg - var(--timer-deg,360deg)),color-mix(in srgb,var(--timer-color,#22bfff) 42%,transparent) 0);opacity:.62;mix-blend-mode:screen;pointer-events:none}.badge>*{position:relative;z-index:1}.winner-burst{position:absolute;left:50%;top:42%;width:26px;height:26px;transform:translate(-50%,-50%);pointer-events:none;z-index:18}.winner-burst:before{content:"";position:absolute;left:50%;top:50%;width:110px;height:110px;border-radius:50%;border:3px solid rgba(255,240,122,.75);box-shadow:0 0 24px #fff07a,0 0 46px #ff6bd6;animation:winnerPulse 1.05s ease-out infinite}.winner-burst i{position:absolute;left:50%;top:50%;width:16px;height:16px;margin-left:-8px;margin-top:-8px;border-radius:50%;background:var(--spark,#fff);box-shadow:0 0 18px var(--spark,#fff),0 0 34px var(--spark,#fff),0 0 54px var(--spark,#fff);animation:sparkBurst 1.05s ease-out infinite;transform:rotate(var(--a)) translate(0)}.winner-burst i:nth-child(1){--a:0deg;--spark:#fff07a}.winner-burst i:nth-child(2){--a:45deg;--spark:#ff6bd6}.winner-burst i:nth-child(3){--a:90deg;--spark:#8dfcff}.winner-burst i:nth-child(4){--a:135deg;--spark:#ffffff}.winner-burst i:nth-child(5){--a:180deg;--spark:#fff07a}.winner-burst i:nth-child(6){--a:225deg;--spark:#ff8aa8}.winner-burst i:nth-child(7){--a:270deg;--spark:#9fffd5}.winner-burst i:nth-child(8){--a:315deg;--spark:#ffffff}@keyframes winnerPulse{0%{opacity:.9;transform:translate(-50%,-50%) scale(.55)}100%{opacity:0;transform:translate(-50%,-50%) scale(1.25)}}@keyframes sparkBurst{0%{opacity:0;transform:rotate(var(--a)) translate(0) scale(.6)}16%{opacity:1}100%{opacity:0;transform:rotate(var(--a)) translate(88px) scale(.2)}}.pname{font-size:clamp(12px,1.05vw,16px);font-weight:1000}.pchips{font-size:clamp(16px,1.4vw,21px);font-weight:1000;color:var(--gold);margin-top:5px}.pstatus{font-size:clamp(9px,.8vw,11px);color:#b7c7d9;margin-top:4px}.rebuy-count{color:#f4d474;font-weight:900}.seat-bet{position:absolute;z-index:6;transform:translate(-50%,-50%)}.player-view{position:absolute;left:53%;bottom:14%;transform:translateX(-50%);z-index:7;text-align:center;min-width:260px}.hand{display:flex;justify-content:center;gap:9px;min-height:94px}.notice{margin-top:8px;min-height:22px;color:#cdeaff;font-size:15px;font-weight:900}.card{position:relative;width:64px;height:88px;background:#f7fbff;color:#111827;border-radius:8px;border:1px solid #d9e0e8;box-shadow:0 8px 15px #0007;flex:0 0 auto}.card.back{background:linear-gradient(135deg,#8f3326,#b84a37);border:3px solid #f4e9df;box-shadow:inset 0 0 0 3px #ffffff33,0 8px 15px #0007}.card.back:after{content:"◇";position:absolute;inset:0;display:grid;place-items:center;color:#ffece094;font-size:34px;font-weight:1000}.corner{position:absolute;left:7px;top:6px;font-size:25px;font-weight:1000;line-height:.86;text-align:center}.corner.bottom{left:auto;top:auto;right:7px;bottom:6px}.suit-big{position:absolute;right:8px;bottom:8px;font-size:42px;line-height:1;font-weight:1000}.small .corner{font-size:16px}.small .suit-big{font-size:26px;right:5px;bottom:5px}.suit-H{color:var(--red)}.suit-D{color:var(--blue)}.suit-C{color:var(--green)}.suit-S{color:#07101c}.actions,.rebuy-panel{grid-column:1;width:min(900px,100%);margin:38px auto 0;border:1px solid var(--line);border-radius:8px;background:rgba(12,17,26,.94);padding:8px;display:grid;gap:7px;align-items:center;min-width:0}.actions{grid-template-columns:repeat(auto-fit,minmax(74px,1fr))}.squid-status-panel,.scoreboard-panel{grid-column:1;width:min(900px,100%);margin:0 auto;border:1px solid rgba(159,243,216,.45);border-radius:8px;background:rgba(5,15,13,.82);box-shadow:0 12px 28px #0006;padding:10px 12px;color:#d8ecff;font-size:14px;font-weight:900;line-height:1.4;text-shadow:0 1px 2px #000}.squid-status-panel b,.scoreboard-panel b{color:#9ff3d8}.squid-status-panel .empty,.scoreboard-panel .empty{color:#93a6bb}.squid-status-panel hr,.scoreboard-panel hr{border:0;border-top:1px solid rgba(255,255,255,.16);margin:8px 0}.score-table{display:grid;grid-template-columns:1.2fr .8fr .8fr .8fr;gap:5px 8px;align-items:center}.score-head{color:#93a6bb;font-size:12px}.score-net-pos{color:#86efac}.score-net-neg{color:#fda4af}.side-stack{grid-column:2;grid-row:2 / span 3;align-self:start;margin-top:clamp(4px,1vh,18px);display:flex;flex-direction:column;gap:10px;min-width:0}.peek-panel{padding:0;gap:0;display:flex;flex-direction:column;border:1px solid var(--line);border-radius:8px;background:rgba(10,15,24,.94);box-shadow:0 18px 40px #0008;overflow:hidden}.peek-summary{padding:10px;display:flex;flex-direction:column;gap:8px;max-height:220px;overflow:auto}.peek-row{border:1px solid #263448;border-radius:7px;background:#101927;padding:8px 9px;font-size:13px;color:#d8ecff}.peek-row b{color:#ffe28a}.check-label{background:rgba(248,250,252,.92);color:#101827;border-color:rgba(255,255,255,.8);text-shadow:none}.actions #raiseAmount{min-width:110px}.rebuy-panel{grid-template-columns:84px 1fr 120px}.center-ready-host{position:absolute;left:76%;top:53%;transform:translate(-50%,-50%);z-index:9;width:210px}.center-ready-host #readyNext{height:58px;font-size:28px;border-radius:8px;padding:10px 16px;box-shadow:0 12px 28px #0009}.actions input,.actions button,.rebuy-panel input,.rebuy-panel button{height:38px;padding:9px 10px;white-space:nowrap;line-height:1.05}.history-panel{border-right:0;padding:0;gap:0;border:1px solid var(--line);border-radius:8px;background:rgba(10,15,24,.94);box-shadow:0 18px 40px #0008;min-height:280px;max-height:min(650px,72vh);overflow:hidden;display:flex;flex-direction:column;min-width:0}.history-title{padding:12px 14px;border-bottom:1px solid #2d3848;font-size:18px;font-weight:1000;color:#ffe28a;display:flex;align-items:center;justify-content:space-between;gap:10px}.history-toggle{width:auto;height:30px;padding:6px 10px;border-radius:999px;font-size:12px;background:#182233;color:#d8ecff;border:1px solid #344154}.history-panel.collapsed{min-height:56px;max-height:56px;width:100%;justify-self:stretch}.history-panel.collapsed .previous-hands,.history-panel.collapsed .history-list{display:none}.previous-hands{padding:8px 10px;border-bottom:1px solid #263448;display:flex;flex-direction:column;gap:6px;max-height:150px;overflow:auto}.previous-hand{border:1px solid #263448;border-radius:7px;background:#0e1725;padding:7px 9px;font-size:12px;color:#cfe3fa}.previous-hand b{color:#f8fafc}.squid-settings{margin-top:0}.squid-line{color:#9ff3d8;font-size:13px;font-weight:900}.history-list{padding:10px;overflow:auto;display:flex;flex-direction:column;gap:8px}.history-row{border:1px solid #263448;border-radius:7px;background:#101927;padding:9px 10px}.history-main{font-size:14px;font-weight:1000;color:#f7fbff}.history-sub{margin-top:4px;font-size:12px;color:#aebed0}.history-stack{color:#f6d365;font-weight:900}.history-empty{padding:12px;color:#9fb0c3;font-weight:800}.hidden{display:none!important}@media(max-width:1250px){.main{grid-template-columns:minmax(640px,1fr);overflow:auto}.side-stack{grid-column:1;grid-row:auto}.history-panel{max-height:260px}.table-shell{max-width:100%}}@media(max-width:760px){.app{grid-template-columns:1fr}aside{border-right:0;border-bottom:1px solid #263140}.main{min-height:auto}.table-shell{min-width:560px}.main{overflow-x:auto}.history-panel{min-height:220px}.actions,.rebuy-panel{min-width:560px}}
.screen{min-height:100vh;display:grid;place-items:center;padding:24px}.panel{width:min(480px,calc(100vw - 32px));border:1px solid var(--line);border-radius:8px;background:rgba(10,15,24,.96);box-shadow:0 18px 45px #0008;padding:18px;display:flex;flex-direction:column;gap:12px}.panel h2{margin:0;font-size:26px}.panel-sub{color:#a8b8ca;font-weight:800;line-height:1.35}.lobby{min-height:100vh;padding:22px;display:grid;grid-template-columns:minmax(320px,460px) minmax(420px,760px);gap:18px;align-content:start;justify-content:center}.lobby-head{grid-column:1/-1;display:flex;justify-content:space-between;align-items:center}.room-list{display:flex;flex-direction:column;gap:10px}.room-row{border:1px solid #2c3848;border-radius:8px;background:#101927;padding:12px;display:grid;grid-template-columns:1fr 112px;gap:10px;align-items:center}.room-name{font-size:18px;font-weight:1000}.room-meta{margin-top:4px;color:#a8b8ca;font-size:13px;font-weight:800}.mini{height:38px;padding:8px 10px}.retired-control{display:none!important}.join-prompt,.action-confirm{position:fixed;inset:0;display:grid;place-items:center;background:rgba(0,0,0,.58);z-index:20;padding:20px}.action-confirm{position:absolute;border-radius:8px}.action-confirm .panel{width:min(390px,calc(100% - 32px))}.join-dialog{width:min(420px,calc(100vw - 32px))}.prompt-actions{display:grid;grid-template-columns:1fr 1fr;gap:10px}.link-btn{background:#182233;color:#d8ecff;border:1px solid #344154}.app.hidden,.screen.hidden,.lobby.hidden{display:none!important}@media(max-width:900px){.lobby{grid-template-columns:1fr}.lobby-head{align-items:flex-start;gap:10px;flex-direction:column}}</style></head><body><section id="loginPage" class="screen"><div class="panel"><h2>Texas Poker Online</h2><div class="panel-sub">Sign in with your player name, password, and invite code.</div><input id="loginName" maxlength="18" placeholder="Username"><input id="loginPassword" type="password" maxlength="32" placeholder="Password"><input id="inviteCode" type="password" maxlength="32" placeholder="Invite code"><button id="loginBtn">Log In</button><div id="loginNotice" class="notice"></div></div></section><section id="lobbyPage" class="lobby hidden"><div class="lobby-head"><div><h1>Texas Poker Online</h1><div id="lobbyUser" class="panel-sub"></div></div><button id="logoutBtn" class="link-btn mini">Log Out</button></div><div class="panel"><h2>Create Room</h2><input id="newRoomName" maxlength="32" placeholder="Room name"><input id="newRoomPassword" type="password" maxlength="32" placeholder="Room password"><button id="createLobbyRoom">Create Room</button><div id="lobbyNotice" class="notice"></div></div><div class="panel"><h2>Current Rooms</h2><button id="refreshRooms" class="link-btn mini">Refresh Rooms</button><div id="roomList" class="room-list"></div></div><div id="joinPrompt" class="join-prompt hidden"><div class="panel join-dialog"><h2>Join Room</h2><div id="joinPromptRoom" class="panel-sub"></div><input id="joinPromptPassword" type="password" maxlength="32" placeholder="Room password"><div class="prompt-actions"><button id="cancelJoinPrompt" class="link-btn mini">Cancel</button><button id="confirmJoinPrompt" class="mini">Join</button></div></div></div></section><div id="gamePage" class="app hidden"><aside><h1>Texas Poker Online</h1><button id="backLobby" class="link-btn">Back to Lobby</button><input id="name" class="retired-control" maxlength="18" placeholder="Player name" value="1"><input id="playerPassword" class="retired-control" type="password" maxlength="32" placeholder="Player password"><input id="roomPassword" class="retired-control" type="password" maxlength="32" placeholder="Room password"><button id="create" class="retired-control">Create Room</button><div class="row retired-control"><input id="roomCode" maxlength="5" placeholder="Room code"><button id="join">Join</button></div><button id="joinSeat">Join Game Next Hand</button><button id="startHand">Start Next Hand</button><button id="shuffleSeats" class="link-btn">Shuffle Seats</button><div class="settings"><div class="label wide">Hand settings</div><label class="input-field"><span>Small blind</span><input id="smallBlind" type="number" min="1" value="5" title="Small blind"></label><label class="input-field"><span>Big blind</span><input id="bigBlind" type="number" min="2" value="5" title="Big blind"></label><label class="input-field"><span>Action clock</span><input id="actionTimeout" type="number" min="5" value="15" title="Action clock seconds"></label><label class="input-field"><span>One buy-time</span><input id="buyTimeSeconds" type="number" min="1" value="30" title="One buy-time seconds"></label><label class="input-field"><span>See min</span><input id="peekPriceMin" type="number" min="0" value="2" title="Minimum wanna see see price"></label><label class="input-field"><span>See max</span><input id="peekPriceMax" type="number" min="0" value="10" title="Maximum wanna see see price"></label><button id="setBlinds" class="wide">Set Blinds / Clock</button></div><div class="settings squid-settings"><div class="label wide">Squid game</div><label class="input-field"><span>Squids</span><input id="squidTotal" type="number" min="0" value="0" title="Squids"></label><label class="input-field"><span>Price</span><input id="squidPrice" type="number" min="0" value="0" title="Price per squid"></label><button id="setSquid" class="wide">Set Squid Game</button></div><div id="meta" class="meta">Create or join a room.</div></aside><main class="main"><div id="pokerGod" class="poker-god"><div>Poker God</div><div id="pokerGodChips">0</div></div><div id="chatPanel" class="chat-panel"><div class="chat-title">Chat</div><div id="chatMessages" class="chat-messages"></div><div class="chat-compose"><input id="chatInput" maxlength="180" placeholder="Message"><button id="chatSend" type="button">Send</button></div></div><div id="topbar" class="topbar"></div><section class="table-shell"><div class="table"></div><div id="seats" class="seats"></div><div id="board" class="board"></div><div class="pot"><div id="potLabel" class="pot-label">Total Pot: 0</div><div class="chips"><span class="chip green"></span><span class="chip red"></span><span class="chip gold"></span><span class="chip blue"></span></div><div id="clock" class="clock"></div><div id="tableNotice" class="table-notice"></div></div><div id="centerReadyHost" class="center-ready-host hidden"><button id="readyNext" class="ghost">Ready</button></div><div id="boardChoiceHint" class="board-choice-hint hidden"></div><div class="player-view"><button id="buyTime" class="buy-time hidden">Buy Time</button><div class="self-hand-row"><div id="hand" class="hand"></div><button id="showEndedHand" class="buy-time show-hand-side hidden">Show Hand</button></div><div id="notice" class="notice"></div></div></section><div class="side-stack"><aside id="historyPanel" class="history-panel"><div class="history-title"><span>Betting History</span><button id="historyToggle" class="history-toggle" type="button">Hide</button></div><div id="previousHands" class="previous-hands"></div><div id="historyList" class="history-list"><div class="history-empty">No betting yet.</div></div></aside><aside id="peekPanel" class="peek-panel"><div class="history-title"><span>Wanna See See</span></div><div id="peekSummary" class="peek-summary"><div class="history-empty">No peeks yet.</div></div></aside></div><section class="actions"><button id="fold" class="ghost">Fold</button><button id="check" class="ghost">Check</button><button id="call">Call</button><button id="allIn" class="ghost">All-In</button><button id="noStraddle" class="ghost">No Straddle</button><button id="straddle">Straddle</button><input id="raiseAmount" type="number" min="1" placeholder="Raise to"><button id="raise">Raise</button></section><section id="rebuyPanel" class="rebuy-panel hidden"><span>Refill</span><input id="rebuyUnits" type="number" value="0" step="1"><button id="rebuy">Apply Refill</button></section><section id="squidStatus" class="squid-status-panel hidden"></section><div id="allInConfirm" class="action-confirm hidden"><div class="panel"><h2>Lovely action!</h2><div class="panel-sub">But you're sure to all-in?</div><div class="prompt-actions"><button id="allInCancel" class="link-btn mini">Cancel</button><button id="allInConfirmBtn" class="mini">All-In</button></div></div></div><section id="scoreboardPanel" class="scoreboard-panel"></section></main></div><script>
const $=id=>document.getElementById(id),els={loginPage:$("loginPage"),lobbyPage:$("lobbyPage"),gamePage:$("gamePage"),loginName:$("loginName"),loginPassword:$("loginPassword"),inviteCode:$("inviteCode"),loginBtn:$("loginBtn"),loginNotice:$("loginNotice"),lobbyUser:$("lobbyUser"),logoutBtn:$("logoutBtn"),newRoomName:$("newRoomName"),newRoomPassword:$("newRoomPassword"),createLobbyRoom:$("createLobbyRoom"),lobbyNotice:$("lobbyNotice"),joinPrompt:$("joinPrompt"),joinPromptRoom:$("joinPromptRoom"),joinPromptPassword:$("joinPromptPassword"),cancelJoinPrompt:$("cancelJoinPrompt"),confirmJoinPrompt:$("confirmJoinPrompt"),refreshRooms:$("refreshRooms"),roomList:$("roomList"),backLobby:$("backLobby"),name:$("name"),playerPassword:$("playerPassword"),roomPassword:$("roomPassword"),create:$("create"),roomCode:$("roomCode"),join:$("join"),joinSeat:$("joinSeat"),startHand:$("startHand"),shuffleSeats:$("shuffleSeats"),smallBlind:$("smallBlind"),bigBlind:$("bigBlind"),actionTimeout:$("actionTimeout"),buyTimeSeconds:$("buyTimeSeconds"),peekPriceMin:$("peekPriceMin"),peekPriceMax:$("peekPriceMax"),setBlinds:$("setBlinds"),squidTotal:$("squidTotal"),squidPrice:$("squidPrice"),setSquid:$("setSquid"),meta:$("meta"),pokerGod:$("pokerGod"),pokerGodChips:$("pokerGodChips"),chatPanel:$("chatPanel"),chatMessages:$("chatMessages"),chatInput:$("chatInput"),chatSend:$("chatSend"),topbar:$("topbar"),seats:$("seats"),board:$("board"),potLabel:$("potLabel"),clock:$("clock"),tableNotice:$("tableNotice"),centerReadyHost:$("centerReadyHost"),boardChoiceHint:$("boardChoiceHint"),squidStatus:$("squidStatus"),scoreboardPanel:$("scoreboardPanel"),peekSummary:$("peekSummary"),previousHands:$("previousHands"),historyList:$("historyList"),historyPanel:$("historyPanel"),historyToggle:$("historyToggle"),buyTime:$("buyTime"),showEndedHand:$("showEndedHand"),hand:$("hand"),notice:$("notice"),fold:$("fold"),check:$("check"),call:$("call"),allIn:$("allIn"),noStraddle:$("noStraddle"),straddle:$("straddle"),raiseAmount:$("raiseAmount"),raise:$("raise"),rebuyPanel:$("rebuyPanel"),readyNext:$("readyNext"),rebuyUnits:$("rebuyUnits"),rebuy:$("rebuy"),allInConfirm:$("allInConfirm"),allInCancel:$("allInCancel"),allInConfirmBtn:$("allInConfirmBtn")};
const playerColors=["#7dd3fc","#fda4af","#86efac","#c4b5fd","#fb923c","#5eead4","#f0abfc","#93c5fd","#f472b6","#a78bfa"];const suitMap={S:"\u2660",H:"\u2665",D:"\u2666",C:"\u2663"},seatPositions=[[53,97],[17,79],[9,53],[17,27],[35,9],[53,9],[71,9],[89,27],[97,53],[89,79]],tableCenter=[53,53];function radialStyle(pos,distance){const tagX=66,tagY=45,dx=tableCenter[0]-pos[0],dy=tableCenter[1]-pos[1],len=Math.hypot(dx,dy)||1;return `left:${Math.round(tagX+(dx/len)*distance)}px;top:${Math.round(tagY+(dy/len)*distance)}px`;}let roomCode=roomCodeFromPath()||localStorage.getItem("roomCode")||"",playerId=localStorage.getItem("playerId")||sessionStorage.getItem("playerId")||"",ws=null,latest=null,timeoutSentAt=0,clockDeadline=0,creatingRoom=false,joiningRoom=false,pendingJoinRoomCode="",settingsDirty=false,appliedSettingsKey="",squidSettingsDirty=false,appliedSquidKey="",lastSoundCount=0;const sounds={bet:new Audio("/audios/betting.mp3"),check:new Audio("/audios/check.mp3"),fold:new Audio("/audios/fold.mp3")};function playSound(kind){const audio=sounds[kind];if(!audio)return;try{audio.currentTime=0;audio.play().catch(()=>{})}catch(e){}}if(roomCode)els.roomCode.value=roomCode;els.join.disabled=false;
function roomCodeFromPath(){const match=location.pathname.match(new RegExp("^/room/([^/]+)$"));return match?decodeURIComponent(match[1]).toUpperCase():""}function setPath(path){if(location.pathname!==path)history.pushState(null,"",path)}function settingsKey(){return `${els.smallBlind.value}|${els.bigBlind.value}|${els.actionTimeout.value}|${els.buyTimeSeconds.value}|${els.peekPriceMin.value}|${els.peekPriceMax.value}`}function squidKey(){return `${els.squidTotal.value}|${els.squidPrice.value}`}function updateSettingsButton(unlocked=true){els.setBlinds.disabled=!unlocked||!settingsDirty}function updateSquidButton(unlocked=true){els.setSquid.disabled=!unlocked||!squidSettingsDirty}function authName(){return localStorage.getItem("authName")||""}function authPassword(){return localStorage.getItem("authPassword")||""}function setIdentity(){els.name.value=authName();els.playerPassword.value=authPassword();els.lobbyUser.textContent=authName()?`Signed in as ${authName()}`:""}function showLogin(){setPath("/");els.loginPage.classList.remove("hidden");els.lobbyPage.classList.add("hidden");els.gamePage.classList.add("hidden");closeJoinPrompt()}function showLobby(){setPath("/lobby");setIdentity();els.loginPage.classList.add("hidden");els.lobbyPage.classList.remove("hidden");els.gamePage.classList.add("hidden");closeJoinPrompt();loadRooms()}function showGame(){if(roomCode)setPath(`/room/${encodeURIComponent(roomCode)}`);setIdentity();els.loginPage.classList.add("hidden");els.lobbyPage.classList.add("hidden");els.gamePage.classList.remove("hidden");closeJoinPrompt()}function openJoinPrompt(code,name,locked){pendingJoinRoomCode=code;els.joinPromptRoom.textContent=`${name||code} ${locked?"requires a room password.":"does not require a room password."}`;els.joinPromptPassword.value="";els.joinPromptPassword.placeholder=locked?"Room password":"Room password (optional)";els.joinPrompt.classList.remove("hidden");setTimeout(()=>els.joinPromptPassword.focus(),0)}function closeJoinPrompt(){pendingJoinRoomCode="";if(els.joinPrompt)els.joinPrompt.classList.add("hidden")}async function loadRooms(){try{const res=await fetch("/rooms"),data=await res.json(),rooms=data.rooms||[];els.roomList.innerHTML=rooms.length?rooms.map(r=>`<div class="room-row"><div><div class="room-name">${esc(r.name)} ${r.hasPassword?"[Locked]":""}</div><div class="room-meta">${String(r.stage).toUpperCase()} | Seats ${r.players}/${r.maxSeats}</div></div><button class="mini" data-room="${r.roomCode}" data-name="${esc(r.name)}" data-locked="${r.hasPassword?1:0}">Join</button></div>`).join(""):`<div class="history-empty">No rooms yet.</div>`;els.roomList.querySelectorAll("button[data-room]").forEach(btn=>btn.onclick=()=>openJoinPrompt(btn.dataset.room,btn.dataset.name,btn.dataset.locked==="1"));}catch(err){els.roomList.innerHTML=`<div class="history-empty">Could not load rooms.</div>`}}async function createLobbyRoom(){if(!authName()){showLogin();return}try{els.lobbyNotice.textContent="Creating room...";const res=await fetch("/rooms",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:authName(),playerPassword:authPassword(),roomName:els.newRoomName.value||"",password:els.newRoomPassword.value||""})}),data=await res.json();if(!res.ok)throw new Error(data.error||"Could not create room");roomCode=data.roomCode;playerId=data.playerId;localStorage.setItem("roomCode",roomCode);localStorage.setItem("playerId",playerId);sessionStorage.setItem("playerId",playerId);els.roomCode.value=roomCode;els.roomPassword.value=els.newRoomPassword.value||"";showGame();connect()}catch(err){els.lobbyNotice.textContent=err.message||"Could not create room"}}async function joinLobbyRoom(code,password=""){if(!authName()){showLogin();return}try{const res=await fetch(`/rooms/${code}/join`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:authName(),playerPassword:authPassword(),password:password||"",playerId:code===roomCode?(playerId||""):""})}),data=await res.json();if(!res.ok)throw new Error(data.error||"Could not join room");roomCode=data.roomCode;playerId=data.playerId;localStorage.setItem("roomCode",roomCode);localStorage.setItem("playerId",playerId);sessionStorage.setItem("playerId",playerId);els.roomCode.value=roomCode;els.roomPassword.value=password||"";closeJoinPrompt();showGame();connect()}catch(err){els.lobbyNotice.textContent=err.message||"Could not join room"}}
function cardEl(card,small=false){if(!card)return `<div class="card back ${small?"small":""}"></div>`;const suit=card.slice(-1),rank=card.slice(0,-1),sym=suitMap[suit]||suit;return `<div class="card ${small?"small":""}"><div class="corner suit-${suit}">${rank}<br>${sym}</div><div class="suit-big suit-${suit}">${sym}</div></div>`}
function send(action,extra={}){if(ws&&ws.readyState===WebSocket.OPEN)ws.send(JSON.stringify({action,...extra}))}
function connect(){if(!roomCode||!playerId)return;if(ws)ws.close();const proto=location.protocol==="https:"?"wss":"ws";ws=new WebSocket(`${proto}://${location.host}/ws/${roomCode}/${playerId}`);ws.onmessage=e=>{const p=JSON.parse(e.data);if(p.type==="state")render(p);if(p.type==="error")els.notice.textContent=p.message}}
function timerProgress(){if(!latest||!latest.state||!clockDeadline)return 1;const started=latest.state.actionStartedAt?Math.round(latest.state.actionStartedAt*1000):0,total=started?Math.max(1,(clockDeadline-started)/1000):Math.max(1,Number(latest.state.actionTimeout)||1),left=Math.max(0,(clockDeadline-Date.now())/1000);return Math.max(0,Math.min(1,left/total))}function updateBadgeTimers(){const deg=`${Math.round(timerProgress()*360)}deg`;document.querySelectorAll(".badge.active").forEach(b=>b.style.setProperty("--timer-deg",deg))}function updateClock(){if(!latest||!latest.state){els.clock.textContent="";updateBadgeTimers();return}const s=latest.state;if(!clockDeadline){els.clock.textContent=s.timeLeft!==null?`Clock: ${s.timeLeft}s`:"";updateBadgeTimers();return}const left=Math.max(0,Math.ceil((clockDeadline-Date.now())/1000));els.clock.textContent=`Clock: ${left}s`;updateBadgeTimers();if(left<=0&&Date.now()-timeoutSentAt>2500){timeoutSentAt=Date.now();send("timeout_fold")}}
function tipText(t){return String(t||"").replace(new RegExp("[.]\\s+","g"),"."+String.fromCharCode(10))}function splitSquidText(t){const lines=tipText(t).split(String.fromCharCode(10)),main=[],squid=[];for(const line of lines){(/squid/i.test(line)?squid:main).push(line)}return {main:main.join(String.fromCharCode(10)),squid:squid.join(String.fromCharCode(10))}}function esc(t){return String(t||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]))}function isNameChar(ch){return /[A-Za-z0-9_-]/.test(ch||"")}function colorNotice(text,colorByName){const items=[["Poker God","#ffe28a"],...Object.entries(colorByName||{})].filter(([name])=>name).sort((a,b)=>b[0].length-a[0].length);let out="",i=0,raw=String(text||"");while(i<raw.length){let hit=null;for(const [name,color] of items){if(raw.slice(i,i+name.length)===name&&!isNameChar(raw[i-1])&&!isNameChar(raw[i+name.length])){hit=[name,color];break}}if(hit){out+=`<span style="color:${hit[1]}">${esc(hit[0])}</span>`;i+=hit[0].length}else{out+=esc(raw[i]);i++}}return out.split(String.fromCharCode(10)).join("<br>")}
function render(payload){latest=payload;const s=payload.state,you=payload.you,seatedRaw=s.players.filter(p=>!p.isSpectator).slice(0,s.maxSeats||10),youSeat=seatedRaw.findIndex(p=>p.id===you.id),seated=youSeat>=0?[...seatedRaw.slice(youSeat),...seatedRaw.slice(0,youSeat)]:seatedRaw;const roomLabel=s.roomName||s.roomCode;els.topbar.textContent=roomCode?`Connected to ${roomLabel}${you.isSpectator?" as spectator":""}`:"";els.meta.textContent=`Room ${roomLabel} | ${s.stage.toUpperCase()} | Blinds ${s.smallBlind}/${s.bigBlind} | Clock ${s.actionTimeout}s | Seats ${seated.length}/${s.maxSeats}`;els.potLabel.textContent=`Total Pot: ${s.pot}`;els.pokerGodChips.textContent=s.pokerGodChips||0;clockDeadline=s.actionDeadline?Math.round(s.actionDeadline*1000):0;updateClock();els.notice.textContent="";els.boardChoiceHint.textContent="";els.boardChoiceHint.classList.add("hidden");const messageParts=splitSquidText(s.message||(you.isSpectator?"Spectating.":""));const playerColorByName=Object.fromEntries((s.players||[]).map(p=>[p.name,playerColors[(p.colorIndex||0)%playerColors.length]]));const boardChoiceActive=!!(s.boardChoice&&s.boardChoice.active);els.tableNotice.innerHTML=boardChoiceActive?"":colorNotice(messageParts.main,playerColorByName);if(boardChoiceActive&&messageParts.main){els.boardChoiceHint.innerHTML=colorNotice(messageParts.main,playerColorByName);els.boardChoiceHint.classList.remove("hidden");}const history=s.handHistory||[];if(history.length<lastSoundCount)lastSoundCount=0;if(lastSoundCount&&history.length>lastSoundCount){history.slice(lastSoundCount).forEach(h=>{const action=String(h.action||"").toLowerCase();if(action==="check")playSound("check");else if(action==="fold"||action==="timeout fold")playSound("fold");else if(/^(small blind|big blind|call|raise|all-in|call all-in|straddle|buy time)$/.test(action))playSound("bet")})}lastSoundCount=history.length;els.historyList.innerHTML=history.length?history.slice().reverse().map(h=>{const color=playerColorByName[h.player]||"#ffe28a",stack=h.stackBefore!==null&&h.stackBefore!==undefined?` <span class="history-stack">(${h.stackBefore} -> ${h.stackAfter})</span>`:"";return `<div class="history-row" style="border-left:4px solid ${color}"><div class="history-main"><b style="color:${color}">${h.player}</b> ${h.action}${h.amount?` ${h.amount}`:""}${stack}</div><div class="history-sub">${String(h.stage).toUpperCase()} | ${h.thinkingTime}s | ${h.time}</div></div>`}).join(""):`<div class="history-empty">No betting yet.</div>`;const peek=s.peekSummary||{},peekRows=peek.actions||[];els.peekSummary.innerHTML=peekRows.length?`<div class="peek-row"><b>Total</b> ${peek.count||0} peek(s), ${peek.total||0} chips to Poker God</div>`+peekRows.slice().reverse().map(p=>`<div class="peek-row"><b>${esc(p.player)}</b> paid ${p.amount}<br>${esc(p.note)}</div>`).join(""):`<div class="history-empty">No peeks yet.</div>`;const boardRows=s.scoreboard||[];els.scoreboardPanel.innerHTML=`<b>Win / Lose</b><div class="score-table"><div class="score-head">Player</div><div class="score-head">Chips</div><div class="score-head">Buy-ins</div><div class="score-head">Net</div>${boardRows.map(r=>`<div>${esc(r.name)}</div><div>${r.chips}</div><div>${r.buyIns}</div><div class="${r.net>=0?"score-net-pos":"score-net-neg"}">${r.net>=0?"+":""}${r.net}</div>`).join("")}</div>`;const chats=s.chatMessages||[];els.chatMessages.innerHTML=chats.length?chats.map(m=>{const chatColor=playerColorByName[m.player]||"#f7fbff";return `<div class="chat-row"><b style="color:${chatColor}">${esc(m.player)}</b> ${esc(m.text)}</div>`}).join(""):`<div class="chat-row">No messages yet.</div>`;els.chatMessages.scrollTop=els.chatMessages.scrollHeight;const prev=s.previousHands||[];els.previousHands.innerHTML=prev.length?prev.slice().reverse().map(h=>{const stacks=(h.stacks||[]).map(p=>`${esc(p.name)} ${p.before} -> ${p.after}`).join(" | ");return `<div class="previous-hand"><b>${(h.winners||[]).map(w=>`${w.name} +${w.amount}`).join(", ")||"No winner"}</b><br>${h.endedAt}${stacks?`<br>${stacks}`:""}<br>${h.path||""}</div>`}).join(""):"";if(s.bankrollAudit){els.meta.textContent+=` | Bankroll ${s.bankrollAudit.live}/${s.bankrollAudit.effectiveBuyIns}`;}if(s.squid&&s.squid.active){els.meta.textContent+=` | Squids ${s.squid.claimed}/${s.squid.total} @ ${s.squid.price}`;}const squidActive=!!(s.squid&&s.squid.active),squidBlocks=[];if(squidActive){const holders=(s.squid.holders||[]).map(h=>`${h.name}: ${h.squids}`).join(" | ")||"None yet",pending=s.squid.pendingPlayer?`<br>First claim reserved for: ${esc(s.squid.pendingPlayer)}`:"";squidBlocks.push(`<b>Squid Status</b><br>Claimed: ${s.squid.claimed} | Unclaimed: ${s.squid.remaining} | Price: ${s.squid.price}${pending}<br>Holders: ${holders}`);}if(messageParts.squid){squidBlocks.push(esc(messageParts.squid).replace(/\\n/g,"<br>"));}els.squidStatus.classList.toggle("hidden",!squidBlocks.length);els.squidStatus.innerHTML=squidBlocks.join("<hr>");const canConfigureSquid=!!(s.squid&&s.squid.canConfigure);els.squidTotal.disabled=!canConfigureSquid;els.squidPrice.disabled=!canConfigureSquid;els.squidTotal.min=s.squid&&s.squid.minimum?s.squid.minimum:0;if(!squidSettingsDirty){els.squidTotal.value=s.squid?s.squid.total:0;els.squidPrice.value=s.squid?s.squid.price:0;appliedSquidKey=squidKey();}updateSquidButton(canConfigureSquid);if(!settingsDirty){els.smallBlind.value=s.smallBlind;els.bigBlind.value=s.bigBlind;els.actionTimeout.value=s.actionTimeout;els.buyTimeSeconds.value=s.buyTimeSeconds||30;els.peekPriceMin.value=s.peekPriceMin||0;els.peekPriceMax.value=s.peekPriceMax||0;appliedSettingsKey=`${s.smallBlind}|${s.bigBlind}|${s.actionTimeout}|${s.buyTimeSeconds||30}|${s.peekPriceMin||0}|${s.peekPriceMax||0}`;}els.bigBlind.min=els.smallBlind.value;els.startHand.disabled=!s.canStartHand;els.shuffleSeats.disabled=!you.canShuffleSeats;els.joinSeat.disabled=!you.canRequestSeat;els.joinSeat.textContent=you.wantsSeat?"Seat Requested":"Join Game Next Hand";els.join.disabled=false;const settingsUnlocked=s.stage==="lobby"||s.stage==="showdown";updateSettingsButton(settingsUnlocked);els.smallBlind.disabled=!settingsUnlocked;els.bigBlind.disabled=!settingsUnlocked;els.actionTimeout.disabled=!settingsUnlocked;els.buyTimeSeconds.disabled=!settingsUnlocked;els.peekPriceMin.disabled=!settingsUnlocked;els.peekPriceMax.disabled=!settingsUnlocked;els.rebuyPanel.classList.toggle("hidden",!!you.isSpectator);els.readyNext.disabled=!you.canReady||!!you.readyNextHand;els.readyNext.textContent="Ready";const showCenterReady=!you.isSpectator&&(s.stage==="lobby"||s.stage==="showdown");els.centerReadyHost.classList.toggle("hidden",!showCenterReady);const minReturn=Math.max(-Math.floor((you.chips||0)/1000),-(you.rebuyUnits||0));els.rebuyUnits.min=minReturn;els.rebuyUnits.placeholder=`Min ${minReturn}`;if(you.refillSubmitted)els.rebuyUnits.value=you.pendingRebuyUnits||0;els.rebuyUnits.disabled=!!you.refillSubmitted;if(Number(els.rebuyUnits.value)<minReturn)els.rebuyUnits.value=minReturn;els.rebuy.textContent=you.refillSubmitted?"Submitted":"Apply Refill";els.rebuy.disabled=!!you.refillSubmitted||!!you.isSpectator;const canChooseBoards=!!you.canChooseBoards,boards=s.communityBoards||[],boardCards=s.communityCards||s.community||[],maxWin=Math.max(0,...(s.winners||[]).map(w=>Number(w.amount)||0)),winnerIds=new Set((s.winners||[]).filter(w=>maxWin>0&&(Number(w.amount)||0)===maxWin).map(w=>w.id));els.board.classList.toggle("multi",boards.length>1);els.board.innerHTML=boards.length>1?boards.map((b,i)=>`<div class="board-row"><span class="board-tag">Board ${i+1}</span>${b.map(c=>cardEl(c)).join("")}</div>`).join(""):(boardCards.length?boardCards:[null,null,null,null,null]).map(c=>cardEl(c)).join("");els.buyTime.classList.toggle("hidden",!you.canAct);els.buyTime.disabled=!you.canBuyTime;els.buyTime.textContent=`Buy Time (${s.buyTimePrice||0})`;els.showEndedHand.classList.toggle("hidden",!you.canShowEndedHand);els.showEndedHand.disabled=!you.canShowEndedHand;els.hand.classList.toggle("folded",!!you.folded);els.hand.innerHTML=((you.cards&&you.cards.length)?you.cards:[null,null]).map(c=>cardEl(c)).join("");els.seats.innerHTML=seated.map((p,i)=>{const pos=seatPositions[i]||[50,50],leftSide=pos[0]<35,rightSide=pos[0]>65,topSide=pos[1]<30,active=p.id===s.currentPlayerId?"active":"",self=p.id===you.id?"self":"",cards=(p.cards||[null,null]).map(c=>cardEl(c,true)).join(""),status=`${p.role||"Online"}${p.folded?" Folded":""}${p.allIn?" All-in":""}${p.connected?"":" Away"}`.trim()||"Online",cardStyle=topSide?"left:34px;top:-44px":leftSide?"left:-56px;top:24px":rightSide?"left:128px;top:24px":"left:34px;top:78px",betStyle=i===0?"left:-42px;top:-18px":radialStyle(pos,96),tokenStyle=i===0?"left:14px;top:-18px":radialStyle(pos,62),color=playerColors[(p.colorIndex||0)%playerColors.length],bet=p.currentBet>0?`<div class="seat-bet" style="${betStyle}"><span class="bet-label"><span class="chip red"></span>${p.currentBet}</span></div>`:(p.checked?`<div class="seat-bet" style="${betStyle}"><span class="check-label"><span class="chip white"></span>Check</span></div>`:"");const canOfferPeek=!!(p.canPeek||(you.folded&&!you.isSpectator&&p.id!==you.id&&p.cardCount>0&&!p.cardsVisible&&!p.privatePeek&&["straddle","preflop","flop","turn","river","board_choice","reveal","optional_show"].includes(s.stage))),peek=canOfferPeek?`<button class="peek-btn" data-peek="${p.id}">wanna see see</button>`:"",burst=winnerIds.has(p.id)?`<div class="winner-burst"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div>`:"",boardChoice=p.id===you.id&&canChooseBoards?`<div class="board-choice-pop">${[1,2,3,4].map(n=>`<button data-board-choice="${n}" ${n>(you.boardChoiceMax||s.boardChoice?.max||1)?"disabled":""}>${n}</button>`).join("")}</div>`:"";let token="";if((p.role||"").includes("Dealer"))token=`<div class="seat-token dealer" style="${tokenStyle}">D</div>`;else if((p.role||"").includes("Small Blind"))token=`<div class="seat-token sb" style="${tokenStyle}">SB</div>`;else if((p.role||"").includes("Big Blind"))token=`<div class="seat-token bb" style="${tokenStyle}">BB</div>`;return `<div class="seat ${self} ${p.folded?"folded":""}" style="left:${pos[0]}%;top:${pos[1]}%"><div class="seat-cards" style="${cardStyle}">${cards}</div>${token}<div class="badge ${active}" style="border-color:${color}99;--timer-color:${color};--timer-deg:360deg"><div class="pname" style="color:${color}">${p.name}</div><div class="pchips">${p.chips}</div><div class="pstatus">${status}<br><span class="rebuy-count">Buy-in: ${p.effectiveRebuyUnits??p.rebuyUnits??1}</span>${p.squids?`<br><span class="squid-line">Squid: ${p.squids}</span>`:""}${peek}</div>${burst}</div>${boardChoice}${bet}</div>`}).join("");updateBadgeTimers();els.seats.querySelectorAll("[data-board-choice]").forEach(btn=>btn.onclick=e=>{e.stopPropagation();send("choose_boards",{count:Number(btn.dataset.boardChoice||1)})});els.seats.querySelectorAll("[data-peek]").forEach(btn=>btn.onclick=e=>{e.stopPropagation();send("peek_hand",{targetId:btn.dataset.peek})});const canAct=!!you.canAct,canStraddle=!!you.canStraddle,canShowdown=!!you.canShowdownAct,canOptionalShow=!!you.canOptionalShow,callNeed=Math.max(0,s.currentBet-(you.currentBet||0));els.fold.disabled=!canAct;els.check.disabled=!canAct||callNeed>0;els.call.disabled=!canAct||callNeed<=0;els.allIn.disabled=!canAct;els.raise.disabled=!canAct;els.raiseAmount.disabled=!canAct;els.noStraddle.disabled=!(canStraddle||canShowdown||canOptionalShow);els.straddle.disabled=!(canStraddle||canShowdown||canOptionalShow);els.noStraddle.textContent=(canShowdown||canOptionalShow)?(canOptionalShow?"Don't Show":"Muck"):"No Straddle";const canClaimSquidNow=!!(you.canOptionalShow&&s.players.find(p=>p.id===you.id&&p.canClaimSquid));els.straddle.textContent=(canShowdown||canOptionalShow)?(canOptionalShow?(canClaimSquidNow?"Show + Squid":"Show"):"Show"):(s.straddleAmount?`Straddle ${s.straddleAmount}`:"Straddle");els.call.textContent=callNeed?`Call ${callNeed}`:"Call";const minRaiseTo=Math.max((s.currentBet||0)+(s.minRaise||s.bigBlind),s.bigBlind);els.raise.textContent="Raise";els.raiseAmount.min=minRaiseTo;els.raiseAmount.placeholder=`Raise to ${minRaiseTo}`;if(canChooseBoards){els.fold.disabled=true;els.check.disabled=true;els.call.disabled=true;els.allIn.disabled=true;els.noStraddle.disabled=true;els.straddle.disabled=true;els.raise.disabled=true;els.raiseAmount.disabled=true;els.boardChoiceHint.innerHTML=(els.boardChoiceHint.innerHTML?els.boardChoiceHint.innerHTML+"<br>":"")+`Choose boards beside your name tag.<br>Lowest player choice decides.<br>Max ${you.boardChoiceMax||s.boardChoice?.max||1}.`;els.boardChoiceHint.classList.remove("hidden");}}
async function createRoom(){if(creatingRoom)return;creatingRoom=true;try{els.meta.textContent="Creating room...";els.create.disabled=true;const res=await fetch("/rooms",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:els.name.value||"Player",password:els.roomPassword.value||"",playerPassword:els.playerPassword.value||""})}),data=await res.json();if(!res.ok||!data.roomCode||!data.playerId){throw new Error(data.error||data.detail||"Could not create room")}roomCode=data.roomCode;playerId=data.playerId;localStorage.setItem("roomCode",roomCode);localStorage.setItem("playerId",playerId);sessionStorage.setItem("playerId",playerId);els.roomCode.value=roomCode;els.meta.textContent=`Created room ${roomCode}.`;connect()}catch(err){els.notice.textContent=err.message||"Could not create room";els.meta.textContent=`Create failed: ${err.message||"Could not create room"}`;}finally{creatingRoom=false;els.create.disabled=false}}
function normalizeRoomCode(value){const raw=(value||"").trim().toUpperCase();const match=raw.match(/[A-Z0-9]{5}/);return match?match[0]:raw}
async function joinRoom(){if(joiningRoom)return;joiningRoom=true;try{const code=normalizeRoomCode(els.roomCode.value);els.roomCode.value=code;if(!code){els.notice.textContent="Enter a room code.";els.meta.textContent="Enter a room code.";return}const idForJoin=code===roomCode?(playerId||""):"";const res=await fetch(`/rooms/${code}/join`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:els.name.value||"Player",playerId:idForJoin,password:els.roomPassword.value||"",playerPassword:els.playerPassword.value||""})}),data=await res.json();if(!res.ok){const msg=data.error||data.detail||"Could not join room";els.notice.textContent=msg;els.meta.textContent=`Join failed: ${msg}`;return}roomCode=data.roomCode;playerId=data.playerId;localStorage.setItem("roomCode",roomCode);localStorage.setItem("playerId",playerId);sessionStorage.setItem("playerId",playerId);els.meta.textContent=`Joined room ${roomCode}.`;connect()}catch(err){els.notice.textContent=err.message||"Could not join room";els.meta.textContent=`Join failed: ${err.message||"Could not join room"}`;}finally{joiningRoom=false}}
els.loginBtn.onclick=async()=>{const name=(els.loginName.value||"").trim(),password=els.loginPassword.value||"",inviteCode=els.inviteCode.value||"";try{els.loginNotice.textContent="Logging in...";const res=await fetch("/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({username:name,password,inviteCode})}),data=await res.json();if(!res.ok)throw new Error(data.error||"Could not log in");localStorage.setItem("authName",data.username||name);localStorage.setItem("authPassword",password);els.loginNotice.textContent="";showLobby()}catch(err){els.loginNotice.textContent=err.message||"Could not log in"}};els.logoutBtn.onclick=()=>{localStorage.removeItem("authName");localStorage.removeItem("authPassword");showLogin()};els.backLobby.onclick=()=>{if(ws)ws.close();roomCode="";playerId="";localStorage.removeItem("roomCode");localStorage.removeItem("playerId");sessionStorage.removeItem("playerId");showLobby();setTimeout(loadRooms,300)};window.onpopstate=()=>{const pathRoom=roomCodeFromPath();if(pathRoom&&authName()){roomCode=pathRoom;els.roomCode.value=roomCode;showGame();connect()}else if(authName()){showLobby()}else{showLogin()}};els.cancelJoinPrompt.onclick=closeJoinPrompt;els.confirmJoinPrompt.onclick=()=>joinLobbyRoom(pendingJoinRoomCode,els.joinPromptPassword.value||"");els.joinPromptPassword.onkeydown=e=>{if(e.key==="Enter")joinLobbyRoom(pendingJoinRoomCode,els.joinPromptPassword.value||"")};els.refreshRooms.onclick=loadRooms;els.createLobbyRoom.onclick=createLobbyRoom;els.historyToggle.onclick=()=>{els.historyPanel.classList.toggle("collapsed");els.historyToggle.textContent=els.historyPanel.classList.contains("collapsed")?"Show":"Hide"};els.create.onclick=createRoom;els.join.onclick=joinRoom;els.startHand.onclick=()=>send("start_hand");els.shuffleSeats.onclick=()=>send("shuffle_seats");els.joinSeat.onclick=()=>send("request_seat");function markSettingsDirty(){settingsDirty=settingsKey()!==appliedSettingsKey;updateSettingsButton(latest?latest.state.stage==="lobby"||latest.state.stage==="showdown":true)}els.smallBlind.oninput=()=>{els.bigBlind.min=els.smallBlind.value;if(Number(els.bigBlind.value)<Number(els.smallBlind.value))els.bigBlind.value=els.smallBlind.value;markSettingsDirty()};els.bigBlind.oninput=markSettingsDirty;els.actionTimeout.oninput=markSettingsDirty;els.buyTimeSeconds.oninput=markSettingsDirty;els.peekPriceMin.oninput=()=>{if(Number(els.peekPriceMax.value)<Number(els.peekPriceMin.value))els.peekPriceMax.value=els.peekPriceMin.value;markSettingsDirty()};els.peekPriceMax.oninput=markSettingsDirty;els.setBlinds.onclick=()=>{settingsDirty=false;appliedSettingsKey=settingsKey();updateSettingsButton(latest?latest.state.stage==="lobby"||latest.state.stage==="showdown":true);send("set_blinds",{smallBlind:Number(els.smallBlind.value),bigBlind:Number(els.bigBlind.value),actionTimeout:Number(els.actionTimeout.value),buyTimeSeconds:Number(els.buyTimeSeconds.value),peekPriceMin:Number(els.peekPriceMin.value),peekPriceMax:Number(els.peekPriceMax.value)})};function markSquidDirty(){squidSettingsDirty=squidKey()!==appliedSquidKey;updateSquidButton(latest&&latest.state&&latest.state.squid?latest.state.squid.canConfigure:true)}els.squidTotal.oninput=markSquidDirty;els.squidPrice.oninput=markSquidDirty;els.setSquid.onclick=()=>{squidSettingsDirty=false;appliedSquidKey=squidKey();updateSquidButton(latest&&latest.state&&latest.state.squid?latest.state.squid.canConfigure:true);send("set_squid",{total:Number(els.squidTotal.value),price:Number(els.squidPrice.value)})};els.fold.onclick=()=>send("fold");els.check.onclick=()=>send("check");els.call.onclick=()=>send("call");function closeAllInConfirm(){els.allInConfirm.classList.add("hidden")}els.allIn.onclick=()=>{if(!els.allIn.disabled)els.allInConfirm.classList.remove("hidden")};els.allInCancel.onclick=closeAllInConfirm;els.allInConfirmBtn.onclick=()=>{closeAllInConfirm();send("all_in")};els.allInConfirm.onclick=e=>{if(e.target===els.allInConfirm)closeAllInConfirm()};els.buyTime.onclick=()=>send("buy_time");els.showEndedHand.onclick=()=>send("show_ended_hand");els.noStraddle.onclick=()=>send(latest&&latest.you&&(latest.you.canShowdownAct||latest.you.canOptionalShow)?"muck_hand":"no_straddle");els.straddle.onclick=()=>send(latest&&latest.you&&(latest.you.canShowdownAct||latest.you.canOptionalShow)?"show_hand":"straddle");els.raise.onclick=()=>send("raise",{amount:Number(els.raiseAmount.value||0)});els.readyNext.onclick=()=>send("ready_next_hand");els.rebuy.onclick=()=>send("rebuy",{units:Number(els.rebuyUnits.value||0)});function sendChat(){const text=(els.chatInput.value||"").trim();if(!text)return;els.chatInput.value="";send("chat",{text})}els.chatSend.onclick=sendChat;els.chatInput.onkeydown=e=>{if(e.key==="Enter")sendChat()};setInterval(updateClock,1000);if(authName()){const pathRoom=roomCodeFromPath();if(pathRoom){roomCode=pathRoom;els.roomCode.value=roomCode;showGame();connect()}else{showLobby()}}else{showLogin()}
</script></body></html>
"""
























