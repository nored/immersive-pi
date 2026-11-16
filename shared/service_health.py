"""systemd notifications and bounded diagnostic event history."""
import datetime
import json
import os
import socket
from collections import deque
from pathlib import Path


def notify(message):
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as connection:
            connection.connect(address)
            connection.sendall(message.encode())
    except OSError:
        pass


class EventLog:
    def __init__(self, directory=None, capacity=200):
        self.events = deque(maxlen=capacity)
        self.path = Path(directory) / "events.jsonl" if directory else None
        if self.path and self.path.exists():
            try:
                for line in self.path.read_text().splitlines()[-capacity:]:
                    self.events.append(json.loads(line))
            except (OSError, ValueError):
                pass

    def record(self, kind, node=None, **evidence):
        event = {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 "kind": kind, "node": node, **evidence}
        self.events.append(event)
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > 2 * 1024 * 1024:
                    self.path.replace(self.path.with_suffix(".previous.jsonl"))
                with self.path.open("a") as stream:
                    stream.write(json.dumps(event) + "\n")
            except OSError as exc:
                print(f"[health] could not persist diagnostic event: {exc}")
        return event
