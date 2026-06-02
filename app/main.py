import hashlib
import json
import os
import random
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
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="Texas Poker Online")

SUITS = ["S", "H", "D", "C"]
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
RANK_VALUES = {rank: index + 2 for index, rank in enumerate(RANKS)}
STARTING_CHIPS = 1000
SMALL_BLIND = 10
BIG_BLIND = 20
MAX_SEATS = 10
DEFAULT_ACTION_TIMEOUT = 60
BETTING_STAGES = ["preflop", "flop", "turn", "river"]
APP_ROOT = Path(__file__).resolve().parent.parent
HISTORY_ROOT = APP_ROOT / "hand_history"
ROOM_STATE_ROOT = Path(os.environ.get("POKER_STATE_DIR", APP_ROOT / "room_state"))
ROOM_STATE_VERSION = 1


def password_hash(password: str) -> str:
    password = password.strip()
    if not password:
        return ""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def make_room_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=5))


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
    cards_visible: bool = False
    squids: int = 0

    def can_act(self) -> bool:
        return not self.is_spectator and not self.folded and not self.all_in and self.chips > 0 and bool(self.cards)


@dataclass
class Room:
    code: str
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
    optional_show_player_id: str | None = None
    optional_show_board_count: int = 0
    pending_result_logged: bool = False
    squid_total: int = 0
    squid_price: int = 0
    squid_claimed: int = 0
    squid_active: bool = False
    squid_settlements: list[dict[str, Any]] = field(default_factory=list)

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
        count = {"lobby": 0, "straddle": 0, "preflop": 0, "flop": 3, "turn": 4, "river": 5, "reveal": 5, "showdown": 5}.get(self.stage, 0)
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
        self.hand_id = f"{stamp.strftime('%Y%m%d_%H%M%S')}_{self.code}"
        self.hand_started_at = stamp.isoformat(timespec="seconds")
        self.action_log = []
        self.last_action_started_at = None

    def action_thinking_time(self) -> float:
        if self.last_action_started_at is None:
            return 0.0
        return round(max(0.0, time.time() - self.last_action_started_at), 1)

    def log_action(self, player: Player, action: str, amount: int = 0, note: str = "", thinking_time: float | None = None, stack_before: int | None = None, stack_after: int | None = None) -> None:
        self.action_log.append({
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
        })

    def log_result(self) -> None:
        if not self.winners:
            return
        summary = "; ".join(f"{winner['name']} wins {winner['amount']} with {winner['hand']}" for winner in self.winners if winner.get("amount", 0) > 0)
        self.action_log.append({
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
        })

    def save_hand_history(self) -> None:
        if not self.hand_id:
            return
        folder = HISTORY_ROOT / self.hand_id
        folder.mkdir(parents=True, exist_ok=True)
        payload = {
            "roomCode": self.code,
            "handId": self.hand_id,
            "startedAt": self.hand_started_at,
            "endedAt": datetime.now().isoformat(timespec="seconds"),
            "smallBlind": self.small_blind,
            "bigBlind": self.big_blind,
            "communityCards": self.community_cards,
            "winners": self.winners,
            "actions": self.action_log,
        }
        (folder / "betting_history.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        summary = {
            "handId": self.hand_id,
            "endedAt": payload["endedAt"],
            "winners": self.winners,
            "path": str(folder / "betting_history.json"),
        }
        self.previous_hands = [hand for hand in self.previous_hands if hand.get("handId") != self.hand_id]
        self.previous_hands.append(summary)
        self.previous_hands = self.previous_hands[-20:]

    def public_state(self) -> dict[str, Any]:
        players = self.ordered_players()
        seated = self.seated_players()
        return {
            "roomCode": self.code,
            "stage": self.stage,
            "pot": self.pot,
            "currentBet": self.current_bet,
            "minRaise": self.min_raise,
            "smallBlind": self.small_blind,
            "bigBlind": self.big_blind,
            "actionTimeout": self.action_timeout,
            "timeLeft": self.current_time_left(),
            "actionDeadline": self.action_deadline,
            "cardsRevealed": self.cards_revealed,
            "straddleAmount": self.straddle_amount,
            "dealerIndex": self.dealer_index,
            "turnIndex": self.turn_index,
            "currentPlayerId": self.optional_show_player_id if self.stage == "optional_show" else (self.current_showdown_player_id() if self.stage == "reveal" else (seated[self.straddle_index].id if self.stage == "straddle" and seated else (seated[self.turn_index].id if self.stage in BETTING_STAGES and seated else None))),
            "communityCards": self.visible_community(),
            "community": self.visible_community(),
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
                    "cards": player.cards if (player.cards_visible and self.stage in {"reveal", "optional_show", "showdown"}) else [],
                    "readyNextHand": player.ready_next_hand,
                    "rebuyUnits": player.rebuy_units,
                    "squids": player.squids,
                    "role": self.player_role(player),
                    "colorIndex": players.index(player) % 10,
                    "isDealer": (not player.is_spectator and seated.index(player) == self.dealer_index) if player in seated else False,
                    "isTurn": ((self.stage in BETTING_STAGES and player in seated and seated.index(player) == self.turn_index) or (self.stage == "straddle" and player in seated and seated.index(player) == self.straddle_index) or (self.stage == "reveal" and self.current_showdown_player_id() == player.id) or (self.stage == "optional_show" and self.optional_show_player_id == player.id)),
                }
                for player in players
            ],
        }

    def private_state_for(self, player_id: str) -> dict[str, Any]:
        player = self.players[player_id]
        call_amount = max(0, self.current_bet - player.current_bet)
        show_cards = bool(player.cards) and (player.cards_visible or player.folded or (not player.folded and (self.cards_revealed or self.stage in {"reveal", "optional_show", "showdown"})))
        return {
            "type": "state",
            "state": self.public_state(),
            "you": {
                "id": player.id,
                "name": player.name,
                "chips": player.chips,
                "folded": player.folded,
                "cards": player.cards if show_cards else [],
                "isSpectator": player.is_spectator,
                "wantsSeat": player.wants_seat,
                "canRequestSeat": player.is_spectator,
                "canAct": self.can_player_act(player_id),
                "canStraddle": self.can_player_straddle(player_id),
                "canShowdownAct": self.can_player_showdown_act(player_id),
                "canOptionalShow": self.can_player_optional_show(player_id),
                "callAmount": min(call_amount, player.chips),
                "minRaiseTo": self.current_bet + self.min_raise,
                "readyNextHand": player.ready_next_hand,
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
        return "Waiting for refill decision: " + ", ".join(pending) + "."

    def can_player_act(self, player_id: str) -> bool:
        seated = self.seated_players()
        return self.stage in BETTING_STAGES and bool(seated) and seated[self.turn_index].id == player_id and self.players[player_id].can_act()

    def can_player_straddle(self, player_id: str) -> bool:
        seated = self.seated_players()
        return self.stage == "straddle" and bool(seated) and seated[self.straddle_index].id == player_id and self.players[player_id].can_act()

    def current_showdown_player_id(self) -> str | None:
        if self.stage != "reveal" or self.showdown_index >= len(self.showdown_order):
            return None
        return self.showdown_order[self.showdown_index]

    def can_player_showdown_act(self, player_id: str) -> bool:
        return self.current_showdown_player_id() == player_id

    def can_player_optional_show(self, player_id: str) -> bool:
        return self.stage == "optional_show" and self.optional_show_player_id == player_id

    def configure_squid(self, total: int, price: int) -> str | None:
        if self.squid_active:
            return "Finish the current squid game before starting a new one."
        if total < 0 or price < 0:
            return "Squid count and price cannot be negative."
        self.squid_total = total
        self.squid_price = price
        self.squid_claimed = 0
        self.squid_active = total > 0 and price > 0
        self.squid_settlements = []
        for player in self.seated_players():
            player.squids = 0
        self.message = f"New squid game set: {total} squids at {price} each." if self.squid_active else "Squid game cleared."
        return None

    def maybe_award_squid(self, player: Player) -> str:
        if not self.squid_active or self.squid_claimed >= self.squid_total:
            return ""
        player.squids += 1
        self.squid_claimed += 1
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
        self.squid_active = False
        self.squid_total = 0
        self.squid_claimed = 0
        self.squid_price = 0
        for player in seated:
            player.squids = 0
        if settlements:
            summary = "\n".join(f"{item['from']} pays {item['to']} {item['amount']}" for item in settlements)
            return "\nSquid game ended.\n" + summary + "\nSquids reset to 0."
        return "\nSquid game ended with no payments.\nSquids reset to 0."

    def configure_blinds(self, small_blind: int, big_blind: int, action_timeout: int | None = None) -> str | None:
        if self.stage not in {"lobby", "showdown"}:
            return "Blinds and clock can be changed after the hand ends."
        if small_blind <= 0 or big_blind < small_blind:
            return "Big blind must be at least the small blind."
        self.small_blind = small_blind
        self.big_blind = big_blind
        self.min_raise = big_blind
        if action_timeout is not None:
            self.action_timeout = max(5, action_timeout)
        timing = "now" if self.stage == "lobby" else "for the next hand"
        self.message = f"Blinds set to {small_blind}/{big_blind} {timing}. Clock: {self.action_timeout}s."
        return None
    def can_start_hand(self) -> bool:
        seated_with_chips = [player for player in self.seated_players() if player.chips > 0 and player.connected]
        if len(seated_with_chips) < 2:
            return False
        if self.stage == "showdown":
            return all(player.ready_next_hand or not player.connected for player in self.seated_players())
        return self.stage == "lobby"

    def request_seat(self, player_id: str) -> str | None:
        player = self.players[player_id]
        if not player.is_spectator:
            return "You are already seated."
        if self.seated_count() >= MAX_SEATS:
            return "Table is full."
        player.wants_seat = True
        player.ready_next_hand = True
        if self.stage in {"lobby", "showdown"}:
            player.is_spectator = False
            player.wants_seat = False
            self.message = f"{player.name} joined the table."
        else:
            self.message = f"{player.name} will join next hand."
        return None

    def mark_ready_next_hand(self, player_id: str, rebuy_units: int = 0) -> str | None:
        if self.stage != "showdown":
            return "Rebuy is available after a hand ends."
        player = self.players[player_id]
        units = rebuy_units
        chip_change = units * STARTING_CHIPS
        if player.chips + chip_change < 0:
            return "Cannot return more refill chips than your current stack."
        if units:
            player.chips += chip_change
            player.rebuy_units += units
        player.ready_next_hand = True
        self.message = self.pending_ready_message()
        return None

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
        for player in self.ordered_players():
            if player.is_spectator and player.wants_seat and player.connected and self.seated_count() < MAX_SEATS:
                player.is_spectator = False
                player.wants_seat = False

    def reset_hand(self) -> None:
        if not self.can_start_hand():
            self.message = "Waiting for seated players to apply their next-hand refill."
            return
        self.prepare_next_hand_seats()
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
            player.cards_visible = False
        for player in players:
            player.cards = [self.deck.pop(), self.deck.pop()]
        small_blind_player = players[(self.dealer_index + 1) % len(players)]
        big_blind_player = players[(self.dealer_index + 2) % len(players)] if len(players) > 2 else players[self.dealer_index]
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
        self.begin_hand_history()
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
            player.acted = True
            self.log_action(player, "fold", 0)
            self.message = f"{player.name} folded."
        elif action == "check":
            if call_amount > 0:
                return f"Call {call_amount} or fold."
            player.acted = True
            self.log_action(player, "check", 0)
            self.message = f"{player.name} checked."
        elif action == "call":
            paid = self.contribute(player, call_amount)
            player.acted = True
            self.log_action(player, "call", paid, stack_before=player.chips + paid, stack_after=player.chips)
            self.message = f"{player.name} called {paid}."
        elif action == "all_in":
            paid = self.contribute(player, player.chips)
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
        self.turn_index = self.next_actor_from(self.dealer_index)
        if self.round_complete():
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
        self.message = f"{winner.name} wins {amount} uncontested. Show hand to claim a squid?"

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
            amount = self.pot
            winner.chips += amount
            self.winners = [{"id": winner.id, "name": winner.name, "amount": amount, "hand": "Uncontested"}]
            self.pot = 0
            self.stage = "showdown"
            self.message = f"{winner.name} wins {amount}." + (self.maybe_award_squid(winner) if winner.cards_visible else "")
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
        scores = {player.id: evaluate_best(player.cards + self.community_cards) for player in contenders}
        contribution_levels = sorted({player.total_bet for player in self.seated_players() if player.total_bet > 0})
        previous_level = 0
        awarded: dict[str, dict[str, Any]] = {}
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
            best_score = max(scores[player.id] for player in eligible)
            winners = [player for player in eligible if scores[player.id] == best_score]
            prize = side_pot // len(winners)
            remainder = side_pot % len(winners)
            for index, winner in enumerate(winners):
                amount = prize + (1 if index < remainder else 0)
                winner.chips += amount
                if winner.id not in awarded:
                    awarded[winner.id] = {"id": winner.id, "name": winner.name, "amount": 0, "hand": hand_name(best_score[0])}
                awarded[winner.id]["amount"] += amount
        self.winners = [winner for winner in awarded.values() if winner.get("amount", 0) > 0]
        squid_note = ""
        if len(self.winners) == 1:
            squid_winner = self.players.get(self.winners[0]["id"])
            if squid_winner is not None and squid_winner.cards_visible:
                squid_note = self.maybe_award_squid(squid_winner)
        result_parts = [f"{winner['name']} wins {winner['amount']} with {winner['hand']}" for winner in self.winners]
        self.message = ("; ".join(result_parts) + "." if result_parts else "No chips awarded.") + squid_note
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
                room = room_from_snapshot(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
            self.rooms[room.code.upper()] = room

    def create_room(self, player_name: str, password: str = "", player_password: str = "") -> tuple[Room, Player]:
        code = make_room_code()
        while code in self.rooms:
            code = make_room_code()
        room = Room(code=code, password_hash=password_hash(password))
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

    async def disconnect(self, room: Room, player_id: str) -> None:
        if player_id in room.players:
            room.players[player_id].connected = False
        room.sockets.pop(player_id, None)
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


@app.post("/rooms")
async def create_room(payload: dict[str, str]) -> JSONResponse:
    player_name = payload.get("name", "").strip() or "Player"
    room_password = payload.get("password", "")
    player_password = payload.get("playerPassword", "")
    room, player = manager.create_room(player_name, room_password, player_password)
    return JSONResponse({"roomCode": room.code, "playerId": player.id, "isSpectator": player.is_spectator})


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
            elif action == "set_blinds":
                try:
                    small_blind = int(message.get("smallBlind", SMALL_BLIND))
                    big_blind = int(message.get("bigBlind", BIG_BLIND))
                    action_timeout = int(message.get("actionTimeout", DEFAULT_ACTION_TIMEOUT))
                except (TypeError, ValueError):
                    small_blind = SMALL_BLIND
                    big_blind = BIG_BLIND
                    action_timeout = DEFAULT_ACTION_TIMEOUT
                error = room.configure_blinds(small_blind, big_blind, action_timeout)
            elif action in {"stay", "rebuy"}:
                try:
                    units = int(message.get("units", 0))
                except (TypeError, ValueError):
                    units = 0
                error = room.mark_ready_next_hand(player_id, units if action == "rebuy" else 0)
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
:root{color-scheme:dark;--gold:#f5c84b;--line:#344154;--felt:#176629;--felt2:#295f34;--rim:#edf1f4;--wood:#28160c;--muted:#a8b8ca;--red:#de4c45;--blue:#278ce8;--green:#23965b}*{box-sizing:border-box}body{margin:0;min-height:100vh;overflow-x:hidden;font-family:Inter,"Segoe UI",Arial,sans-serif;background:radial-gradient(circle at 63% 48%,#182033 0,#0a101a 44%,#05080d 100%);color:#f7fbff}.app{min-height:100vh;display:grid;grid-template-columns:clamp(210px,18vw,280px) minmax(0,1fr)}aside{background:#0d121b;border-right:1px solid #263140;padding:clamp(8px,1vw,16px);display:flex;flex-direction:column;gap:10px;min-width:0;overflow:auto}h1{margin:0 0 8px;font-size:24px}input,button{width:100%;border:1px solid #3a4658;border-radius:7px;padding:11px 12px;font:800 15px Inter,"Segoe UI",Arial,sans-serif}input{background:#172131;color:#fff}button{cursor:pointer;background:linear-gradient(#ffd765,#efbd38);color:#090d14;border:0}#join{cursor:pointer;pointer-events:auto}button:disabled{cursor:not-allowed;background:#6f7780;color:#111827;opacity:1}.ghost{background:linear-gradient(#ffd765,#efbd38);color:#090d14}.row{display:grid;grid-template-columns:1fr 62px;gap:8px}.settings{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding-top:10px;border-top:1px solid #2b3544}.wide{grid-column:1/-1}.label{color:var(--muted);font-size:12px;font-weight:900;margin-bottom:-8px}.meta{color:#c9e4ff;font-size:17px;font-weight:1000;line-height:1.45;min-height:60px}.main{position:relative;min-width:0;min-height:100vh;padding:clamp(8px,1.2vw,20px);display:grid;grid-template-columns:minmax(640px,1180px) minmax(260px,360px);grid-template-rows:auto auto auto auto;gap:12px clamp(10px,1.4vw,24px);align-content:start;justify-content:center;overflow:auto}.topbar{grid-column:1/-1;display:flex;justify-content:flex-end;min-height:32px;color:#c5dbf4;font-size:15px;font-weight:900}.table-shell{grid-column:1;position:relative;width:min(1180px,100%);aspect-ratio:1180/650;height:auto;margin:clamp(4px,1vh,18px) auto 0;min-width:640px}.table{position:absolute;left:53%;top:53%;transform:translate(-50%,-50%);width:82%;height:68%;border-radius:245px;background:radial-gradient(circle at 50% 52%,rgba(255,255,255,.08),transparent 36%),linear-gradient(160deg,var(--felt2),var(--felt));border:14px solid var(--rim);box-shadow:0 0 0 12px var(--wood),0 28px 70px rgba(0,0,0,.55),inset 0 0 80px rgba(0,0,0,.28)}.table:after{content:"🉐Evanston";position:absolute;inset:0;display:grid;place-items:center;font-size:52px;font-weight:1000;color:rgba(255,255,255,.12);pointer-events:none}.board{position:absolute;left:53%;top:36%;transform:translate(-50%,-50%);display:flex;gap:9px;z-index:2}.pot{position:absolute;left:53%;top:51%;transform:translate(-50%,-50%);z-index:3;text-align:center}.table-notice{margin-top:6px;min-height:20px;color:#cdeaff;font-size:15px;font-weight:1000;text-shadow:0 1px 2px #000;white-space:pre-line;line-height:1.35}.seat-token{position:absolute;width:28px;height:28px;border-radius:50%;display:grid;place-items:center;font-size:11px;font-weight:1000;border:2px solid rgba(255,255,255,.8);box-shadow:0 4px 10px #0009;z-index:8;transform:translate(-50%,-50%)}.seat-token.dealer{background:#f7fbff;color:#111827}.seat-token.sb{background:#8d5cf6;color:#fff}.seat-token.bb{background:#ffd33f;color:#111827}.pot-label,.bet-label{display:inline-flex;align-items:center;gap:7px;background:rgba(3,10,15,.9);border:1px solid rgba(255,255,255,.24);border-radius:999px;padding:5px 12px;color:#fff8d7;font-weight:1000;font-size:17px;text-shadow:0 1px 0 #000}.chips{margin-top:6px;display:flex;justify-content:center}.chip{width:19px;height:19px;border-radius:50%;border:2px solid rgba(255,255,255,.6);box-shadow:0 2px 4px #0008}.red{background:#d83d37}.green{background:#63bc5c}.gold{background:#f0aa31}.blue{background:#4c7dea}.clock{margin-top:7px;font-size:16px;font-weight:1000;color:#ffe38a}.seats{position:absolute;inset:0;z-index:4;pointer-events:none}.seat{position:absolute;width:132px;height:104px;transform:translate(-50%,-50%);pointer-events:auto}.seat-cards{position:absolute;display:flex}.seat.self .seat-cards{display:none}.seat.folded .badge,.seat.folded .seat-cards,.hand.folded .card{opacity:.42;filter:grayscale(1)}.seat .card{width:42px;height:58px;margin-right:-10px;border-radius:7px}.badge{position:absolute;top:8px;left:50%;transform:translateX(-50%);min-width:92px;min-height:74px;border:1px solid #ffffff33;border-radius:7px;background:rgba(6,10,17,.92);text-align:center;padding:6px 7px;box-shadow:0 12px 28px #0006}.badge.active{border:4px solid #22bfff;box-shadow:0 0 22px #22bfff99}.pname{font-size:clamp(12px,1.05vw,16px);font-weight:1000}.pchips{font-size:clamp(16px,1.4vw,21px);font-weight:1000;color:var(--gold);margin-top:5px}.pstatus{font-size:clamp(9px,.8vw,11px);color:#b7c7d9;margin-top:4px}.rebuy-count{color:#f4d474;font-weight:900}.seat-bet{position:absolute;z-index:6;transform:translate(-50%,-50%)}.player-view{position:absolute;left:53%;bottom:14%;transform:translateX(-50%);z-index:7;text-align:center;min-width:260px}.hand{display:flex;justify-content:center;gap:9px;min-height:94px}.notice{margin-top:8px;min-height:22px;color:#cdeaff;font-size:15px;font-weight:900}.card{position:relative;width:64px;height:88px;background:#f7fbff;color:#111827;border-radius:8px;border:1px solid #d9e0e8;box-shadow:0 8px 15px #0007;flex:0 0 auto}.card.back{background:linear-gradient(135deg,#8f3326,#b84a37);border:3px solid #f4e9df;box-shadow:inset 0 0 0 3px #ffffff33,0 8px 15px #0007}.card.back:after{content:"◇";position:absolute;inset:0;display:grid;place-items:center;color:#ffece094;font-size:34px;font-weight:1000}.corner{position:absolute;left:7px;top:6px;font-size:25px;font-weight:1000;line-height:.86;text-align:center}.corner.bottom{left:auto;top:auto;right:7px;bottom:6px}.suit-big{position:absolute;right:8px;bottom:8px;font-size:42px;line-height:1;font-weight:1000}.small .corner{font-size:16px}.small .suit-big{font-size:26px;right:5px;bottom:5px}.suit-H{color:var(--red)}.suit-D{color:var(--blue)}.suit-C{color:var(--green)}.suit-S{color:#07101c}.actions,.rebuy-panel{grid-column:1;width:min(900px,100%);margin:38px auto 0;border:1px solid var(--line);border-radius:8px;background:rgba(12,17,26,.94);padding:8px;display:grid;gap:7px;align-items:center;min-width:0}.actions{grid-template-columns:repeat(auto-fit,minmax(74px,1fr))}.squid-status-panel{position:absolute;left:68%;top:49%;transform:translateY(-50%);z-index:5;width:min(280px,24%);max-height:190px;overflow:auto;border:1px solid rgba(159,243,216,.45);border-radius:8px;background:rgba(5,15,13,.52);box-shadow:0 12px 28px #0006;padding:10px 12px;color:#d8ecff;font-size:14px;font-weight:900;line-height:1.4;text-shadow:0 1px 2px #000}.squid-status-panel b{color:#9ff3d8}.squid-status-panel .empty{color:#93a6bb}.squid-status-panel hr{border:0;border-top:1px solid rgba(255,255,255,.16);margin:8px 0}.actions #raiseAmount{min-width:110px}.rebuy-panel{grid-template-columns:110px 90px 1fr}.actions input,.actions button,.rebuy-panel input,.rebuy-panel button{height:38px;padding:9px 10px;white-space:nowrap;line-height:1.05}.history-panel{border-right:0;padding:0;gap:0;grid-column:2;grid-row:2 / span 3;align-self:start;margin-top:clamp(4px,1vh,18px);border:1px solid var(--line);border-radius:8px;background:rgba(10,15,24,.94);box-shadow:0 18px 40px #0008;min-height:280px;max-height:min(650px,72vh);overflow:hidden;display:flex;flex-direction:column;min-width:0}.history-title{padding:12px 14px;border-bottom:1px solid #2d3848;font-size:18px;font-weight:1000;color:#ffe28a}.previous-hands{padding:8px 10px;border-bottom:1px solid #263448;display:flex;flex-direction:column;gap:6px;max-height:150px;overflow:auto}.previous-hand{border:1px solid #263448;border-radius:7px;background:#0e1725;padding:7px 9px;font-size:12px;color:#cfe3fa}.previous-hand b{color:#f8fafc}.squid-settings{margin-top:0}.squid-line{color:#9ff3d8;font-size:13px;font-weight:900}.history-list{padding:10px;overflow:auto;display:flex;flex-direction:column;gap:8px}.history-row{border:1px solid #263448;border-radius:7px;background:#101927;padding:9px 10px}.history-main{font-size:14px;font-weight:1000;color:#f7fbff}.history-sub{margin-top:4px;font-size:12px;color:#aebed0}.history-stack{color:#f6d365;font-weight:900}.history-empty{padding:12px;color:#9fb0c3;font-weight:800}.hidden{display:none!important}@media(max-width:1250px){.main{grid-template-columns:minmax(640px,1fr);overflow:auto}.history-panel{grid-column:1;grid-row:auto;max-height:260px}.table-shell{max-width:100%}}@media(max-width:760px){.app{grid-template-columns:1fr}aside{border-right:0;border-bottom:1px solid #263140}.main{min-height:auto}.table-shell{min-width:560px}.main{overflow-x:auto}.history-panel{min-height:220px}.actions,.rebuy-panel{min-width:560px}}
</style></head><body><div class="app"><aside><h1>Texas Poker Online</h1><input id="name" maxlength="18" placeholder="Player name" value="1"><input id="playerPassword" type="password" maxlength="32" placeholder="Player password"><input id="roomPassword" type="password" maxlength="32" placeholder="Room password"><button id="create">Create Room</button><div class="row"><input id="roomCode" maxlength="5" placeholder="Room code"><button id="join">Join</button></div><button id="joinSeat">Join Game Next Hand</button><button id="startHand">Start Next Hand</button><div class="settings"><div class="label wide">Hand settings</div><input id="smallBlind" type="number" min="1" value="10" title="Small blind"><input id="bigBlind" type="number" min="2" value="20" title="Big blind"><input id="actionTimeout" class="wide" type="number" min="5" value="60" title="Action clock seconds"><button id="setBlinds" class="wide">Set Blinds / Clock</button></div><div class="settings squid-settings"><div class="label wide">Squid game</div><input id="squidTotal" type="number" min="0" value="0" title="Squids"><input id="squidPrice" type="number" min="0" value="0" title="Price per squid"><button id="setSquid" class="wide">Set Squid Game</button></div><div id="meta" class="meta">Create or join a room.</div></aside><main class="main"><div id="topbar" class="topbar"></div><section class="table-shell"><div class="table"></div><div id="seats" class="seats"></div><div id="board" class="board"></div><div class="pot"><div id="potLabel" class="pot-label">Total Pot: 0</div><div class="chips"><span class="chip green"></span><span class="chip red"></span><span class="chip gold"></span><span class="chip blue"></span></div><div id="clock" class="clock"></div><div id="tableNotice" class="table-notice"></div></div><div class="player-view"><div id="hand" class="hand"></div><div id="notice" class="notice"></div></div><section id="squidStatus" class="squid-status-panel hidden"></section></section><aside class="history-panel"><div class="history-title">Betting History</div><div id="previousHands" class="previous-hands"></div><div id="historyList" class="history-list"><div class="history-empty">No betting yet.</div></div></aside><section class="actions"><button id="fold" class="ghost">Fold</button><button id="check" class="ghost">Check</button><button id="call">Call</button><button id="allIn" class="ghost">All-In</button><button id="noStraddle" class="ghost">No Straddle</button><button id="straddle">Straddle</button><input id="raiseAmount" type="number" min="1" placeholder="Raise to"><button id="raise">Raise</button></section><section id="rebuyPanel" class="rebuy-panel hidden"><span>Next hand</span><input id="rebuyUnits" type="number" value="0" step="1"><button id="rebuy">Apply Refill</button></section></main></div><script>
const $=id=>document.getElementById(id),els={name:$("name"),playerPassword:$("playerPassword"),roomPassword:$("roomPassword"),create:$("create"),roomCode:$("roomCode"),join:$("join"),joinSeat:$("joinSeat"),startHand:$("startHand"),smallBlind:$("smallBlind"),bigBlind:$("bigBlind"),actionTimeout:$("actionTimeout"),setBlinds:$("setBlinds"),squidTotal:$("squidTotal"),squidPrice:$("squidPrice"),setSquid:$("setSquid"),meta:$("meta"),topbar:$("topbar"),seats:$("seats"),board:$("board"),potLabel:$("potLabel"),clock:$("clock"),tableNotice:$("tableNotice"),squidStatus:$("squidStatus"),previousHands:$("previousHands"),historyList:$("historyList"),hand:$("hand"),notice:$("notice"),fold:$("fold"),check:$("check"),call:$("call"),allIn:$("allIn"),noStraddle:$("noStraddle"),straddle:$("straddle"),raiseAmount:$("raiseAmount"),raise:$("raise"),rebuyPanel:$("rebuyPanel"),rebuyUnits:$("rebuyUnits"),rebuy:$("rebuy")};
const playerColors=["#7dd3fc","#fda4af","#86efac","#c4b5fd","#fb923c","#5eead4","#f0abfc","#93c5fd","#f472b6","#a78bfa"];const suitMap={S:"\u2660",H:"\u2665",D:"\u2666",C:"\u2663"},seatPositions=[[53,97],[17,79],[9,53],[17,27],[35,9],[53,9],[71,9],[89,27],[97,53],[89,79]],tableCenter=[53,53];function radialStyle(pos,distance){const tagX=66,tagY=45,dx=tableCenter[0]-pos[0],dy=tableCenter[1]-pos[1],len=Math.hypot(dx,dy)||1;return `left:${Math.round(tagX+(dx/len)*distance)}px;top:${Math.round(tagY+(dy/len)*distance)}px`;}let roomCode=localStorage.getItem("roomCode")||"",playerId=localStorage.getItem("playerId")||sessionStorage.getItem("playerId")||"",ws=null,latest=null,timeoutSentAt=0,clockDeadline=0,creatingRoom=false,joiningRoom=false;if(roomCode)els.roomCode.value=roomCode;els.join.disabled=false;
function cardEl(card,small=false){if(!card)return `<div class="card back ${small?"small":""}"></div>`;const suit=card.slice(-1),rank=card.slice(0,-1),sym=suitMap[suit]||suit;return `<div class="card ${small?"small":""}"><div class="corner suit-${suit}">${rank}<br>${sym}</div><div class="suit-big suit-${suit}">${sym}</div></div>`}
function send(action,extra={}){if(ws&&ws.readyState===WebSocket.OPEN)ws.send(JSON.stringify({action,...extra}))}
function connect(){if(!roomCode||!playerId)return;if(ws)ws.close();const proto=location.protocol==="https:"?"wss":"ws";ws=new WebSocket(`${proto}://${location.host}/ws/${roomCode}/${playerId}`);ws.onmessage=e=>{const p=JSON.parse(e.data);if(p.type==="state")render(p);if(p.type==="error")els.notice.textContent=p.message}}
function updateClock(){if(!latest||!latest.state){els.clock.textContent="";return}const s=latest.state;if(!clockDeadline){els.clock.textContent=s.timeLeft!==null?`Clock: ${s.timeLeft}s`:"";return}const left=Math.max(0,Math.ceil((clockDeadline-Date.now())/1000));els.clock.textContent=`Clock: ${left}s`;if(left<=0&&Date.now()-timeoutSentAt>2500){timeoutSentAt=Date.now();send("timeout_fold")}}
function tipText(t){return String(t||"").replace(new RegExp("[.]\\s+","g"),"."+String.fromCharCode(10))}function splitSquidText(t){const lines=tipText(t).split(String.fromCharCode(10)),main=[],squid=[];for(const line of lines){(/squid/i.test(line)?squid:main).push(line)}return {main:main.join(String.fromCharCode(10)),squid:squid.join(String.fromCharCode(10))}}function esc(t){return String(t||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]))}
function render(payload){latest=payload;const s=payload.state,you=payload.you,seatedRaw=s.players.filter(p=>!p.isSpectator).slice(0,s.maxSeats||10),youSeat=seatedRaw.findIndex(p=>p.id===you.id),seated=youSeat>=0?[...seatedRaw.slice(youSeat),...seatedRaw.slice(0,youSeat)]:seatedRaw;els.topbar.textContent=roomCode?`Connected to room ${roomCode}${you.isSpectator?" as spectator":""}`:"";els.meta.textContent=`Room ${s.roomCode} | ${s.stage.toUpperCase()} | Blinds ${s.smallBlind}/${s.bigBlind} | Clock ${s.actionTimeout}s | Seats ${seated.length}/${s.maxSeats}`;els.potLabel.textContent=`Total Pot: ${s.pot}`;clockDeadline=s.actionDeadline?Math.round(s.actionDeadline*1000):0;updateClock();els.notice.textContent="";const messageParts=splitSquidText(s.message||(you.isSpectator?"Spectating.":""));els.tableNotice.textContent=messageParts.main;const playerColorByName=Object.fromEntries((s.players||[]).map(p=>[p.name,playerColors[(p.colorIndex||0)%playerColors.length]])),history=s.handHistory||[];els.historyList.innerHTML=history.length?history.slice().reverse().map(h=>{const color=playerColorByName[h.player]||"#ffe28a",stack=h.stackBefore!==null&&h.stackBefore!==undefined?` <span class="history-stack">(${h.stackBefore} -> ${h.stackAfter})</span>`:"";return `<div class="history-row" style="border-left:4px solid ${color}"><div class="history-main"><b style="color:${color}">${h.player}</b> ${h.action}${h.amount?` ${h.amount}`:""}${stack}</div><div class="history-sub">${String(h.stage).toUpperCase()} | ${h.thinkingTime}s | ${h.time}</div></div>`}).join(""):`<div class="history-empty">No betting yet.</div>`;const prev=s.previousHands||[];els.previousHands.innerHTML=prev.length?prev.slice().reverse().map(h=>`<div class="previous-hand"><b>${(h.winners||[]).map(w=>`${w.name} +${w.amount}`).join(", ")||"No winner"}</b><br>${h.endedAt}<br>${h.path||""}</div>`).join(""):"";if(s.squid&&s.squid.active){els.meta.textContent+=` | Squids ${s.squid.claimed}/${s.squid.total} @ ${s.squid.price}`;}const squidActive=!!(s.squid&&s.squid.active),squidBlocks=[];if(squidActive){const holders=(s.squid.holders||[]).map(h=>`${h.name}: ${h.squids}`).join(" | ")||"None yet";squidBlocks.push(`<b>Squid Status</b><br>Claimed: ${s.squid.claimed} | Unclaimed: ${s.squid.remaining} | Price: ${s.squid.price}<br>Holders: ${holders}`);}if(messageParts.squid){squidBlocks.push(esc(messageParts.squid).replace(/\\n/g,"<br>"));}els.squidStatus.classList.toggle("hidden",!squidBlocks.length);els.squidStatus.innerHTML=squidBlocks.join("<hr>");els.setSquid.disabled=squidActive;els.squidTotal.disabled=squidActive;els.squidPrice.disabled=squidActive;els.squidTotal.value=s.squid?s.squid.total:0;els.squidPrice.value=s.squid?s.squid.price:0;els.smallBlind.value=s.smallBlind;els.bigBlind.value=s.bigBlind;els.bigBlind.min=els.smallBlind.value;els.actionTimeout.value=s.actionTimeout;els.startHand.disabled=!s.canStartHand;els.joinSeat.disabled=!you.canRequestSeat;els.joinSeat.textContent=you.wantsSeat?"Seat Requested":"Join Game Next Hand";els.join.disabled=false;const settingsUnlocked=s.stage==="lobby"||s.stage==="showdown";els.setBlinds.disabled=!settingsUnlocked;els.smallBlind.disabled=!settingsUnlocked;els.bigBlind.disabled=!settingsUnlocked;els.actionTimeout.disabled=!settingsUnlocked;els.rebuyPanel.classList.toggle("hidden",!(s.stage==="showdown"&&!you.readyNextHand&&!you.isSpectator));const minReturn=-Math.floor((you.chips||0)/1000);els.rebuyUnits.min=minReturn;els.rebuyUnits.placeholder=`Min ${minReturn}`;if(Number(els.rebuyUnits.value)<minReturn)els.rebuyUnits.value=minReturn;const boardCards=s.communityCards||s.community||[];els.board.innerHTML=(boardCards.length?boardCards:[null,null,null,null,null]).map(c=>cardEl(c)).join("");els.hand.classList.toggle("folded",!!you.folded);els.hand.innerHTML=((you.cards&&you.cards.length)?you.cards:[null,null]).map(c=>cardEl(c)).join("");els.seats.innerHTML=seated.map((p,i)=>{const pos=seatPositions[i]||[50,50],leftSide=pos[0]<35,rightSide=pos[0]>65,topSide=pos[1]<30,active=p.id===s.currentPlayerId?"active":"",self=p.id===you.id?"self":"",cards=(p.cards||[null,null]).map(c=>cardEl(c,true)).join(""),status=`${p.role||"Online"}${p.folded?" Folded":""}${p.allIn?" All-in":""}${p.connected?"":" Away"}`.trim()||"Online",cardStyle=topSide?"left:34px;top:-44px":leftSide?"left:-56px;top:24px":rightSide?"left:128px;top:24px":"left:34px;top:78px",betStyle=i===0?"left:-42px;top:-18px":radialStyle(pos,96),tokenStyle=i===0?"left:14px;top:-18px":radialStyle(pos,62),color=playerColors[(p.colorIndex||0)%playerColors.length],bet=p.currentBet>0?`<div class="seat-bet" style="${betStyle}"><span class="bet-label"><span class="chip red"></span>${p.currentBet}</span></div>`:"";let token="";if((p.role||"").includes("Dealer"))token=`<div class="seat-token dealer" style="${tokenStyle}">D</div>`;else if((p.role||"").includes("Small Blind"))token=`<div class="seat-token sb" style="${tokenStyle}">SB</div>`;else if((p.role||"").includes("Big Blind"))token=`<div class="seat-token bb" style="${tokenStyle}">BB</div>`;return `<div class="seat ${self} ${p.folded?"folded":""}" style="left:${pos[0]}%;top:${pos[1]}%"><div class="seat-cards" style="${cardStyle}">${cards}</div>${token}<div class="badge ${active}" style="border-color:${color}99"><div class="pname" style="color:${color}">${p.name}</div><div class="pchips">${p.chips}</div><div class="pstatus">${status}<br><span class="rebuy-count">Buy-in: ${p.rebuyUnits||1}</span>${p.squids?`<br><span class="squid-line">Squid: ${p.squids}</span>`:""}</div></div>${bet}</div>`}).join("");const canAct=!!you.canAct,canStraddle=!!you.canStraddle,canShowdown=!!you.canShowdownAct,canOptionalShow=!!you.canOptionalShow,callNeed=Math.max(0,s.currentBet-(you.currentBet||0));els.fold.disabled=!canAct;els.check.disabled=!canAct||callNeed>0;els.call.disabled=!canAct||callNeed<=0;els.allIn.disabled=!canAct;els.raise.disabled=!canAct;els.raiseAmount.disabled=!canAct;els.noStraddle.disabled=!(canStraddle||canShowdown||canOptionalShow);els.straddle.disabled=!(canStraddle||canShowdown||canOptionalShow);els.noStraddle.textContent=(canShowdown||canOptionalShow)?(canOptionalShow?"Don't Show":"Muck"):"No Straddle";els.straddle.textContent=(canShowdown||canOptionalShow)?(canOptionalShow?"Show + Squid":"Show"):(s.straddleAmount?`Straddle ${s.straddleAmount}`:"Straddle");els.call.textContent=callNeed?`Call ${callNeed}`:"Call";const minRaiseTo=Math.max((s.currentBet||0)+(s.minRaise||s.bigBlind),s.bigBlind);els.raiseAmount.min=minRaiseTo;els.raiseAmount.placeholder=`Raise to ${minRaiseTo}`}
async function createRoom(){if(creatingRoom)return;creatingRoom=true;try{els.meta.textContent="Creating room...";els.create.disabled=true;const res=await fetch("/rooms",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:els.name.value||"Player",password:els.roomPassword.value||"",playerPassword:els.playerPassword.value||""})}),data=await res.json();if(!res.ok||!data.roomCode||!data.playerId){throw new Error(data.error||data.detail||"Could not create room")}roomCode=data.roomCode;playerId=data.playerId;localStorage.setItem("roomCode",roomCode);localStorage.setItem("playerId",playerId);sessionStorage.setItem("playerId",playerId);els.roomCode.value=roomCode;els.meta.textContent=`Created room ${roomCode}.`;connect()}catch(err){els.notice.textContent=err.message||"Could not create room";els.meta.textContent=`Create failed: ${err.message||"Could not create room"}`;}finally{creatingRoom=false;els.create.disabled=false}}
function normalizeRoomCode(value){const raw=(value||"").trim().toUpperCase();const match=raw.match(/[A-Z0-9]{5}/);return match?match[0]:raw}
async function joinRoom(){if(joiningRoom)return;joiningRoom=true;try{const code=normalizeRoomCode(els.roomCode.value);els.roomCode.value=code;if(!code){els.notice.textContent="Enter a room code.";els.meta.textContent="Enter a room code.";return}const idForJoin=code===roomCode?(playerId||""):"";const res=await fetch(`/rooms/${code}/join`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:els.name.value||"Player",playerId:idForJoin,password:els.roomPassword.value||"",playerPassword:els.playerPassword.value||""})}),data=await res.json();if(!res.ok){const msg=data.error||data.detail||"Could not join room";els.notice.textContent=msg;els.meta.textContent=`Join failed: ${msg}`;return}roomCode=data.roomCode;playerId=data.playerId;localStorage.setItem("roomCode",roomCode);localStorage.setItem("playerId",playerId);sessionStorage.setItem("playerId",playerId);els.meta.textContent=`Joined room ${roomCode}.`;connect()}catch(err){els.notice.textContent=err.message||"Could not join room";els.meta.textContent=`Join failed: ${err.message||"Could not join room"}`;}finally{joiningRoom=false}}
els.create.onclick=createRoom;els.join.onclick=joinRoom;els.startHand.onclick=()=>send("start_hand");els.joinSeat.onclick=()=>send("request_seat");els.smallBlind.oninput=()=>{els.bigBlind.min=els.smallBlind.value;if(Number(els.bigBlind.value)<Number(els.smallBlind.value))els.bigBlind.value=els.smallBlind.value};els.setBlinds.onclick=()=>send("set_blinds",{smallBlind:Number(els.smallBlind.value),bigBlind:Number(els.bigBlind.value),actionTimeout:Number(els.actionTimeout.value)});els.setSquid.onclick=()=>send("set_squid",{total:Number(els.squidTotal.value),price:Number(els.squidPrice.value)});els.fold.onclick=()=>send("fold");els.check.onclick=()=>send("check");els.call.onclick=()=>send("call");els.allIn.onclick=()=>send("all_in");els.noStraddle.onclick=()=>send(latest&&latest.you&&(latest.you.canShowdownAct||latest.you.canOptionalShow)?"muck_hand":"no_straddle");els.straddle.onclick=()=>send(latest&&latest.you&&(latest.you.canShowdownAct||latest.you.canOptionalShow)?"show_hand":"straddle");els.raise.onclick=()=>send("raise",{amount:Number(els.raiseAmount.value||0)});els.rebuy.onclick=()=>send("rebuy",{units:Number(els.rebuyUnits.value||0)});setInterval(updateClock,1000);connect();
</script></body></html>
"""
























