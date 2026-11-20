import asyncio
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import unittest

from app.main import RoomManager, TableUpdateStream, table_update_operations


ROOT = Path(__file__).resolve().parents[1]


class Socket:
    def __init__(self, delta=True):
        self.state = SimpleNamespace()
        if delta:
            self.state.table_updates = TableUpdateStream()
        self.messages = []

    async def send_text(self, message):
        await asyncio.sleep(0)
        self.messages.append(message)


class Room:
    def __init__(self, payload):
        self.payload = payload
        self.sockets = {}

    def private_state_for(self, player_id):
        return self.payload


class TableUpdateTests(unittest.IsolatedAsyncioTestCase):
    def manager(self):
        # Avoid loading or writing the user's saved rooms.
        return RoomManager.__new__(RoomManager)

    async def test_mutable_baseline_noop_resync_and_concurrent_sends(self):
        payload = {"type": "state", "state": {"players": [{"id": "a", "chips": 1000}],
                   "handHistory": [{"text": "x" * 1000}]}, "you": {"id": "a"}}
        room, socket, manager = Room(payload), Socket(), self.manager()
        room.sockets["a"] = socket
        await manager.send_table_state(room, "a", socket)
        await manager.send_table_state(room, "a", socket)
        self.assertEqual(len(socket.messages), 1)
        payload["state"]["players"][0]["chips"] = 950
        await asyncio.gather(*(manager.send_table_state(room, "a", socket) for _ in range(3)))
        delta = json.loads(socket.messages[-1])
        self.assertEqual(delta["type"], "state_delta")
        self.assertEqual(delta["baseRevision"], 1)
        self.assertEqual(delta["revision"], 2)
        self.assertEqual(len(socket.messages), 2)
        await manager.send_table_state(room, "a", socket, force=True)
        snapshot = json.loads(socket.messages[-1])
        self.assertEqual(snapshot["type"], "state")
        self.assertEqual(snapshot["state"], payload["state"])
        self.assertEqual(snapshot["revision"], 3)

    async def test_legacy_socket_receives_complete_state(self):
        payload = {"type": "state", "state": {"pot": 30}, "you": {"id": "old-browser"}}
        socket, room = Socket(delta=False), Room(payload)
        await self.manager().send_table_state(room, "old-browser", socket)
        self.assertEqual(json.loads(socket.messages[-1]), payload)

    @unittest.skipUnless(shutil.which("node"), "Node is required to test the browser receiver")
    async def test_browser_reconstructs_each_update_and_requests_resync(self):
        cases = [
            {"type": "state", "state": {"pot": 0, "players": [{"id": "a", "chips": 1000}],
             "handHistory": ["entry " + str(i) for i in range(80)], "choice": None}},
            {"type": "state", "state": {"pot": 50, "players": [{"id": "a", "chips": 950}],
             "handHistory": ["entry " + str(i) for i in range(1, 81)], "choice": {"a": 2}}},
            {"type": "state", "state": {"pot": 50, "players": [{"id": "a", "chips": 950}, {"id": "b", "chips": 700}],
             "handHistory": ["entry " + str(i) for i in range(1, 81)], "choice": {"a": 2, "b": 1}}},
            {"type": "state", "state": {"pot": 0, "players": [], "handHistory": [], "choice": None}},
        ]
        messages = [{**cases[0], "revision": 1}]
        for index in range(1, len(cases)):
            messages.append({
                "type": "state_delta", "baseRevision": index, "revision": index + 1,
                "operations": table_update_operations(cases[index - 1], cases[index]),
            })
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
vm.runInThisContext(fs.readFileSync('app/table-updates.js', 'utf8'));
let requests = 0;
const receive = createTableUpdateReceiver(() => requests++);
for (let i = 0; i < input.messages.length; i++) {
  const reconstructed = {...receive(input.messages[i])};
  delete reconstructed.revision;
  assert.deepEqual(reconstructed, input.cases[i]);
}
assert.equal(receive({type:'state_delta',baseRevision:999,revision:1000,operations:[]}),null);
assert.equal(receive({type:'state_delta',baseRevision:999,revision:1000,operations:[]}),null);
assert.equal(requests,1);
assert.deepEqual(receive({...input.cases[0],revision:10}),{...input.cases[0],revision:10});
assert.equal(receive({type:'state_delta',baseRevision:10,revision:11,
  operations:[['set',['missing','key'],123]]}),null);
assert.equal(requests,2);
"""
        result = subprocess.run(
            ["node", "-e", script], cwd=ROOT, input=json.dumps({"cases": cases, "messages": messages}),
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
