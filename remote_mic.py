"""Route fresh, authenticated mic commands to one explicitly enabled local tab."""
from collections import OrderedDict
import math
import queue
import threading
import time


class RemoteMicControls:
    def __init__(self):
        self._lock = threading.Lock()
        self._clients = {}
        self._owner = None
        self._seen = OrderedDict()

    def connect(self, client, events):
        if isinstance(client, str) and 1 <= len(client) <= 80:
            with self._lock:
                self._clients[client] = events

    def disconnect(self, events):
        with self._lock:
            for client, stream in list(self._clients.items()):
                if stream is events:
                    del self._clients[client]
                    if self._owner == client:
                        self._owner = None

    def configure(self, client, enabled):
        with self._lock:
            if not isinstance(client, str) or client not in self._clients:
                raise ValueError("Reconnect the Answer Board before enabling remote mic control.")
            if enabled:
                if self._owner not in (None, client):
                    raise ValueError("Remote mic control is enabled in another Answer Board tab. Disable it there first.")
                self._owner = client
            elif self._owner == client:
                self._owner = None
            return {"enabled": self._owner == client}

    def submit(self, payload):
        engine, command_id, sent = payload.get("engine"), payload.get("id"), payload.get("sent_at")
        if engine not in ("free", "paid") or not isinstance(command_id, str) or not 1 <= len(command_id) <= 80:
            raise ValueError("Invalid remote microphone command.")
        if not isinstance(sent, (int, float)) or not math.isfinite(sent) or not -10 <= time.time() - sent <= 30:
            raise ValueError("Remote mic command expired. Check both laptops' clocks and press again.")
        with self._lock:
            if command_id in self._seen:
                return {"mic_command": "duplicate"}
            events = self._clients.get(self._owner)
            if events is None:
                raise ValueError("On the receiving laptop, open Answer Board Settings and enable remote mic control.")
            try:
                events.put_nowait(("mic-command", {"id": command_id, "engine": engine, "expires": sent + 30}))
            except queue.Full:
                raise ValueError("Receiving Answer Board is busy. Reconnect it and try again.") from None
            self._seen[command_id] = None
            if len(self._seen) > 2048:
                self._seen.popitem(last=False)
            return {"mic_command": "queued"}
