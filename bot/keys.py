"""
Live keys (Windows): C = collect positions in profit, X = close everything, P = pause entries,
A = toggle auto-collect. Other systems have no keys (the bot still runs).
"""
import asyncio
import os
import threading


class KeyListener:
    def __init__(self):
        self.queue: "asyncio.Queue[str]" = asyncio.Queue()
        self._thread = None

    def start(self):
        if os.name != "nt" or self._thread is not None:
            return
        loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._read, args=(loop,), daemon=True)
        self._thread.start()

    def _read(self, loop):
        try:
            import msvcrt
            while True:
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):      # special key: discard the second code
                    msvcrt.getwch()
                    continue
                loop.call_soon_threadsafe(self.queue.put_nowait, ch.lower())
        except Exception:
            return                                # no interactive console: no keys

    def poll(self) -> list:
        keys = []
        while not self.queue.empty():
            keys.append(self.queue.get_nowait())
        return keys
