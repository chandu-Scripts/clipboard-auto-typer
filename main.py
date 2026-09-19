import http.server
import json
import os
import queue
import re
import socket
import socketserver
import sys
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from tkinter import filedialog

import keyboard
import pyperclip
import pystray
import pythoncom
import requests
import win32api
import win32com.client
import win32con
import win32gui
import win32process
from PIL import Image, ImageDraw

CLIPBOARD_POLL_SECONDS = 0.15

# Inserted between an old (possibly interrupted, mid-sentence) entry and a
# newly started one, since entries are no longer cleared - a full dashed
# line makes the cutoff point visually obvious rather than just a plain
# blank line.
ENTRY_SEPARATOR = "\n" + "-" * 40 + "\n"
# Chosen by the user (ctrl+alt+p). Known trade-off: a letter key can leak
# through as a real shortcut on a mistimed press - ctrl+alt+p briefly
# looking like ctrl+p = Print happened once, which is why this used to be
# ctrl+alt+insert. Avoid F10 (Windows treats it as a special system key that
# can activate a window's menu bar on its own, which is what opened a new
# untitled file via File > New).
PAUSE_HOTKEY = "ctrl+alt+p"
HOTKEY_DEBOUNCE_SECONDS = 0.5

# Typing is done via Word's COM automation (Document.Content.InsertAfter),
# not simulated keystrokes. Two independent problems with keyboard.write()/
# send() led to this direction generally:
#   1. It only ever reaches whichever window has OS focus at the moment
#      Windows actually delivers the event - not the moment this script
#      checked focus - so switching apps mid-word could leak/corrupt text
#      into the wrong window no matter how often focus was re-checked.
#   2. Measured directly: even with the target window focused the ENTIRE
#      time (no app-switching involved at all), keyboard.write()
#      intermittently drops a character and duplicates the next one (e.g.
#      "my age is" came out as "yy gge is"). This is a known flakiness of
#      the low-level SendInput-based injection keyboard.write() uses, not
#      something a per-keystroke delay reliably fixes.
# This app originally used UI Automation against Notepad instead of Word
# (SetValue() on the whole document each flush, ~500ms/call regardless of
# length) - switched to Word because Notepad's font/zoom display reset
# itself whenever a large automated write landed, with no reliable fix;
# Word's font is a real, persistent document property instead of a
# transient view-level setting, so it doesn't have that problem. As a
# bonus, Word's Range.InsertAfter() is both much cheaper (~30ms measured,
# even after 40 consecutive calls with no growth in cost) and a true
# incremental append, unlike SetValue() needing the whole document
# content re-sent on every call.
WORD_DEFAULT_FONT_NAME = "Consolas"
WORD_DEFAULT_FONT_SIZE = 14
# Word constant for Range.Collapse() - collapses a range to a zero-length
# point at its end. (wdCollapseEnd, hardcoded rather than pulled from
# win32com.client.constants, since that requires generating Word's type
# library wrapper via makepy first - not worth it for one constant.)
WD_COLLAPSE_END = 0
# Measured cost of one InsertAfter()+ScrollIntoView() call (~30ms, stable
# across 40 consecutive calls with no cumulative slowdown as the document
# grows) - AutoTyper batches enough words per call to keep pace with the
# requested WPM without calling more often than this supports. In
# practice this comes out to 1 word per call for any reasonable WPM,
# since Word's COM calls are so much cheaper than the ~500ms Notepad's UI
# Automation SetValue() cost this replaced.
WORD_CALL_INTERVAL_SECONDS = 0.05

# Text copied on a paired sending laptop (see clipboard_to_remote_loop) is
# relayed through ntfy.sh (a free, public pub/sub service - see
# https://ntfy.sh): the sender POSTs to a topic, this app subscribes to
# that same topic over Server-Sent Events. The topic name
# alone is not a real secret (anyone who guesses/finds it could subscribe),
# so every message also carries a shared secret that must match
# remote_config.json - see remote_config.example.json for the format. That
# file is gitignored; it's generated per-install, never committed.
NTFY_BASE_URL = "https://ntfy.sh"
REMOTE_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "remote_config.json")
# Local-only session state (last-used mode/connection/path/speed, and
# whether the toggle was on) - kept separate from remote_config.json,
# which is meant to be copied between the two laptops; "was this laptop
# sending or receiving" is specific to this one, not something to share.
APP_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_settings.json")
# pythonw.exe has no console, so an unhandled exception anywhere - main
# thread, a background thread, or inside a Tkinter callback - would
# otherwise just vanish with zero indication anything went wrong. See
# main()'s sys.excepthook/threading.excepthook wiring and
# App.report_callback_exception.
CRASH_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crash.log")

# "Connected"/"Reachable" used to mean only "this laptop can reach ntfy.sh
# at all", which stayed true even with no laptop on the other end - not a
# real pairing check. Each side now also sends a small heartbeat message
# (kind="heartbeat") over the same topic every HEARTBEAT_INTERVAL_SECONDS
# while its toggle is on; the displayed status only says "Connected" once
# a heartbeat has actually been seen from the other role within
# HEARTBEAT_TIMEOUT_SECONDS (see connection_status_ticker).
HEARTBEAT_INTERVAL_SECONDS = 5
HEARTBEAT_TIMEOUT_SECONDS = 12

# When both laptops are on the same network, they can talk directly
# instead of relaying through ntfy.sh - no internet round-trip, no daily
# message quota. Both sides must agree on this port (stored in
# remote_config.json as "lan_port", defaulting here if absent).
LAN_DEFAULT_PORT = 8765

# The "Answer Board" web page: a text view served from this laptop to a
# browser on this same laptop (bound to 127.0.0.1 only, so nothing else on
# the network can reach it - unlike the LAN listener above). board.html sits
# next to main.py and is read fresh on each page load.
BOARD_PORT = 8766
BOARD_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "board.html")
BOARD_URL = f"http://localhost:{BOARD_PORT}/"
BOARD_WINDOW_TITLE = "Answer Board"  # must match <title> in board.html
BOARD_MAX_CHARS = 2_000_000
BOARD_KEEPALIVE_SECONDS = 15

TOKEN_PATTERN = re.compile(r"\S+|\s+")


def load_remote_config():
    """Loads the shared secret used by both the internet relay (ntfy.sh
    topic) and the direct LAN connection, plus their optional settings
    (lan_port, lan_receiver_ip). Returns None if the file is missing or
    malformed, so remote features are simply unavailable rather than
    crashing the app - the file deliberately isn't committed to git (see
    remote_config.example.json). lan_receiver_ip is specific to whichever
    laptop is doing the sending - it's harmless (just unused) if present
    in a copy of this file on a laptop that's only receiving."""
    if not os.path.exists(REMOTE_CONFIG_PATH):
        return None
    try:
        with open(REMOTE_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        topic, secret = data.get("topic"), data.get("secret")
        if topic and secret:
            return {
                "topic": topic,
                "secret": secret,
                "lan_port": data.get("lan_port", LAN_DEFAULT_PORT),
                "lan_receiver_ip": data.get("lan_receiver_ip", ""),
            }
    except (OSError, ValueError):
        pass
    return None


def save_remote_config(config):
    """Persists remote_config.json (e.g. after the user enters a LAN
    receiver IP, so they don't need to retype it every restart). Silently
    does nothing on failure - this is a convenience, not something the
    app's correctness depends on."""
    try:
        with open(REMOTE_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except OSError:
        pass


def get_local_ip():
    """Best-effort local LAN IP address for this machine (not 127.0.0.1),
    shown to the user so they know what to type into the other laptop's
    "Receiver IP" field. Opens a UDP "connection" to a public address
    purely to ask the OS which local interface it would route through -
    no packet actually needs to be sent for that, and no internet access
    is required for this to work."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def load_app_settings():
    """Loads last-used UI/session state (mode, connection type, Word
    document path, speed, font, and whether each toggle was on) so a
    restart doesn't require re-selecting everything - see
    App.apply_saved_settings. Returns an empty dict (all defaults) if the
    file is missing or malformed, same reasoning as load_remote_config."""
    try:
        with open(APP_SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_app_settings(settings):
    """Persists app_settings.json - see App.autosave_settings_loop. Purely
    a convenience; failures are silent, same as save_remote_config."""
    try:
        with open(APP_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except OSError:
        pass


def log_exception(context, exc_type, exc_value, exc_tb):
    """Appends a timestamped traceback to crash.log - the only way to see
    an unhandled exception at all under pythonw.exe, which has no console
    for stderr to go to. Best-effort: if writing the log itself fails,
    give up rather than raise (this runs inside exception handlers - it
    must never itself throw)."""
    try:
        with open(CRASH_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Unhandled exception in {context}:\n")
            f.writelines(traceback.format_exception(exc_type, exc_value, exc_tb))
    except Exception:
        pass


def log_still_alive():
    """Appends a timestamped heartbeat line to crash.log, independent of
    log_exception. This is what makes the log useful even for a crash
    these hooks can't catch (a native-level crash inside the win32gui/
    Word COM interop, or the process being killed outright by
    Windows/antivirus) - no exception, so nothing else would get logged,
    but the last heartbeat timestamp still narrows down when it died."""
    try:
        with open(CRASH_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] still running\n")
    except OSError:
        pass


def tokenize(text):
    """Splits text into (is_word, chunk) pairs, alternating between runs of
    non-whitespace and runs of whitespace (spaces, tabs, blank lines). Typing
    the chunks back out in order reproduces the original text exactly."""
    return [(not m.group()[0].isspace(), m.group()) for m in TOKEN_PATTERN.finditer(text)]


def get_word_document(file_path, timeout=10.0):
    """Connects to a running Word instance (launching one if none exists)
    and returns the Document object for file_path - opening it if the
    file exists, creating and saving it otherwise. Returns None if this
    can't be done within `timeout` (e.g. Word is still starting up).

    Must be called on a thread that has already called
    pythoncom.CoInitialize() - COM objects have thread affinity (the STA
    apartment model), so a Document reference from one thread can't
    safely be handed to another. Every caller in this app instead calls
    get_word_document() fresh on its own thread; win32com.client.Dispatch
    transparently connects to the SAME already-running Word process and
    Documents collection rather than launching a duplicate (confirmed
    directly: dispatching from a second thread sees the first thread's
    document, same window handle, no new Word window opens)."""
    deadline = time.time() + timeout
    target = os.path.normcase(os.path.abspath(file_path))
    while True:
        try:
            word = win32com.client.Dispatch("Word.Application")
            word.Visible = True
            for d in word.Documents:
                try:
                    if os.path.normcase(d.FullName) == target:
                        return d
                except Exception:
                    continue
            if os.path.exists(file_path):
                return word.Documents.Open(file_path)
            doc = word.Documents.Add()
            doc.SaveAs2(file_path)
            return doc
        except Exception:
            if time.time() >= deadline:
                return None
            time.sleep(0.3)


def apply_word_font(doc, font_name, font_size):
    """Sets the document's actual font - a real, persistent document
    property, unlike Notepad's zoom/display font size, which turned out
    to reset itself whenever a large automated write landed, with no
    reliable fix (see the module comment above WORD_DEFAULT_FONT_NAME).
    Best-effort: typing still works even if this fails for some reason."""
    try:
        doc.Content.Font.Name = font_name
        doc.Content.Font.Size = font_size
    except Exception:
        pass


def scroll_word_to_end(doc):
    """Scrolls the document's window so the very end is visible - called
    after every write (see AutoTyper._run) so newly-typed text stays in
    view as the document grows past one screen. Cheap enough (~30ms
    measured, no growth over 40 consecutive calls) to do inline after
    every flush, unlike the equivalent Notepad operation this app used to
    do on a separate thread to avoid capping typing speed."""
    try:
        end_range = doc.Content
        end_range.Collapse(WD_COLLAPSE_END)
        doc.ActiveWindow.ScrollIntoView(end_range, True)
    except Exception:
        pass  # best-effort - never let a scroll failure interrupt typing


class LANServer(socketserver.ThreadingTCPServer):
    """Plain ThreadingTCPServer, except request-handling errors (a client
    disconnecting mid-response, a malformed request, etc. - see the
    ConnectionResetError this app hit in practice when a slow Word COM
    call outlasted the sending laptop's old, shorter request timeout) go
    to crash.log via log_exception instead of BaseServer's default
    handle_error(), which just prints to stderr - invisible under
    pythonw.exe, which has no console for stderr to go to."""

    def handle_error(self, request, client_address):
        log_exception(f"LAN request from {client_address}", *sys.exc_info())


class LANRequestHandler(http.server.BaseHTTPRequestHandler):
    """Handles direct-LAN messages on the receiving laptop - the local
    equivalent of remote_watch_loop's ntfy.sh subscription, except here
    the sending laptop connects straight to this laptop's own small HTTP
    server instead of both sides talking through a third party. Expects
    `self.server.app_ref` to be set to the owning App instance (done in
    App.toggle_remote_watch when the server is created)."""

    def do_POST(self):
        # ThreadingTCPServer spawns a brand new OS thread for every
        # incoming request (not a reused pool) and none of them have COM
        # initialized - same requirement as every other Word-COM-touching
        # thread in this app (see AutoTyper._run, clipboard_watch_loop,
        # etc.), just easy to miss here since it's the standard library
        # spawning the thread, not code of ours that obviously needed
        # this call added.
        pythoncom.CoInitialize()
        try:
            self._handle_post()
        finally:
            pythoncom.CoUninitialize()

    def _handle_post(self):
        app = self.server.app_ref
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            payload = json.loads(body)
        except (ValueError, TypeError):
            self.send_response(400)
            self.end_headers()
            return
        secret = (app.remote_config or {}).get("secret")
        if not secret or payload.get("secret") != secret:
            self.send_response(403)
            self.end_headers()
            return
        # Any validly-secret-matched request (heartbeat or real text) is
        # proof the sending laptop can reach this one - see
        # connection_status_ticker for how this drives the "Connected"
        # status on this side.
        app.lan_last_request_time = time.time()
        kind = payload.get("kind", "text")
        # Reported in the response body below so the sending laptop's
        # Ctrl+Alt+P hotkey knows this laptop's REAL current pause state,
        # not just its own guess (see App.remote_typer_paused). For a
        # pause/resume command, report the state we just applied rather
        # than re-reading app.paused - remote_set_paused() below runs on
        # the Tk main thread via root.after(), so it may not have actually
        # run yet by the time this response goes out.
        reported_paused = app.paused
        if kind == "text":
            text = payload.get("text", "")
            if text:
                # Handing off to a background thread and responding
                # immediately, rather than awaiting handle_new_clipboard_text
                # here, decouples the sender's request timeout from however
                # long Word's COM calls take (cold Word start, a
                # OneDrive-synced document path, etc. can all push this past
                # a few seconds) - otherwise the sender can give up and
                # close the connection while this thread is still working,
                # which surfaces as a ConnectionResetError on end_headers()
                # below even though the text was already on its way to Word.
                threading.Thread(target=self._start_typing, args=(app, text), daemon=True).start()
        elif kind in ("pause", "resume"):
            should_pause = kind == "pause"
            # remote_set_paused() mirrors this same guard (no job running,
            # or already in the requested state) and no-ops in either
            # case - checked here too so the response doesn't optimistically
            # report a state change that's not actually going to happen.
            # is_running() reads Thread.is_alive(), which is safe to call
            # from this (non-main) thread.
            if app.typer.is_running() and should_pause != app.paused:
                app.root.after(0, lambda sp=should_pause: app.remote_set_paused(sp))
                reported_paused = should_pause
            # else: nothing will change - reported_paused stays app.paused
        response_body = json.dumps({"paused": reported_paused}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)

    def _start_typing(self, app, text):
        pythoncom.CoInitialize()
        try:
            app.handle_new_clipboard_text(text)
        finally:
            pythoncom.CoUninitialize()

    def log_message(self, format, *args):
        pass  # suppress BaseHTTPRequestHandler's default per-request stderr logging


class BoardHub:
    """Holds the Answer Board's current text and fans every change out to
    each connected browser page. Thread-safe: the typing thread appends,
    HTTP handler threads subscribe/clear, and the Tk thread reads state."""

    def __init__(self):
        self._lock = threading.Lock()
        self._text = ""
        self._status = {"state": "idle", "done": 0, "total": 0}
        self._subscribers = []

    def has_text(self):
        with self._lock:
            return bool(self._text.strip())

    def append(self, chunk):
        if not chunk:
            return
        with self._lock:
            self._text += chunk
            if len(self._text) > BOARD_MAX_CHARS:
                self._text = self._text[-BOARD_MAX_CHARS:]
                # Pages hold the untrimmed text - resync them all instead
                # of sending a chunk they'd append to the wrong base.
                self._broadcast_locked("snapshot", self._snapshot_locked())
                return
            self._broadcast_locked("append", chunk)

    def clear(self):
        with self._lock:
            self._text = ""
            self._broadcast_locked("clear", None)

    def set_status(self, state, done=None, total=None):
        with self._lock:
            self._status["state"] = state
            if done is not None:
                self._status["done"] = done
            if total is not None:
                self._status["total"] = total
            self._broadcast_locked("status", dict(self._status))

    def subscribe(self):
        """Returns (queue, snapshot) atomically, so a page never misses or
        double-applies an event between its snapshot and its live stream."""
        q = queue.Queue(maxsize=5000)
        with self._lock:
            self._subscribers.append(q)
            return q, self._snapshot_locked()

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def is_subscribed(self, q):
        with self._lock:
            return q in self._subscribers

    def _snapshot_locked(self):
        return {"text": self._text, "status": dict(self._status)}

    def _broadcast_locked(self, event, data):
        for q in list(self._subscribers):
            try:
                q.put_nowait((event, data))
            except queue.Full:
                # A stuck page must not block typing - drop it. Its stream
                # notices it's no longer subscribed and ends, and the
                # browser reconnects with a fresh snapshot.
                self._subscribers.remove(q)


class BoardServer(LANServer):
    """Serves the Answer Board page and its live text stream on 127.0.0.1
    only. Expects `hub` (a BoardHub) to be set by the creator."""

    daemon_threads = True  # open pages must not keep the app alive at exit

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.hub = None
        self.stopping = threading.Event()

    def handle_error(self, request, client_address):
        # A browser tab closing mid-stream is normal, not worth a crash.log entry.
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class BoardRequestHandler(http.server.BaseHTTPRequestHandler):
    def _host_ok(self):
        # Rejects requests whose Host header isn't this loopback address -
        # stops another website from reading the board through DNS
        # rebinding, even though the server only listens on 127.0.0.1.
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").lower()
        return host in (f"localhost:{port}", f"127.0.0.1:{port}")

    def _reply(self, code, body=b"", content_type="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok():
            self._reply(403, b"Forbidden")
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/board", "/index.html"):
            try:
                with open(BOARD_HTML_PATH, "rb") as f:
                    body = f.read()
            except OSError:
                self._reply(500, b"board.html not found next to main.py")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; connect-src 'self'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)
        elif path == "/stream":
            self._stream()
        elif path == "/favicon.ico":
            self._reply(204)
        else:
            self._reply(404, b"Not found")

    def do_POST(self):
        if not self._host_ok():
            self._reply(403, b"Forbidden")
            return
        path = self.path.split("?", 1)[0]
        # A custom header makes this a non-simple request, which a browser
        # won't let another website send cross-origin.
        if path == "/clear" and self.headers.get("X-Board") == "1":
            self.server.hub.clear()
            self._reply(204)
        else:
            self._reply(404, b"Not found")

    def _send_event(self, event, data):
        payload = json.dumps(data)  # one line: json escapes newlines
        self.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _stream(self):
        hub = self.server.hub
        q, snapshot = hub.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self._send_event("snapshot", snapshot)
            while True:
                try:
                    event, data = q.get(timeout=BOARD_KEEPALIVE_SECONDS)
                except queue.Empty:
                    if self.server.stopping.is_set() or not hub.is_subscribed(q):
                        return
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self._send_event(event, data)
        except OSError:
            pass  # page closed or navigated away
        finally:
            hub.unsubscribe(q)

    def log_message(self, format, *args):
        pass


class AutoTyper:
    """Writes tokenized text into a Word document via COM automation
    (Document.Content.InsertAfter), in word batches paced to approximate
    a controllable WPM speed. Runs on its own thread; pause/resume/stop
    are signaled via threading events so the caller stays responsive.
    Pauses automatically whenever the target window doesn't have OS focus
    (see _wait_until_ready) and resumes exactly where it left off once it
    does - this is a deliberate UX choice, not a technical requirement:
    because writes happen as atomic COM calls rather than individual
    keystrokes, this pause is corruption-free by construction, unlike the
    old keystroke-based approach. Pausing while unfocused means the user
    can freely type in another app without any background writes landing
    there or competing for input. (Confirmed directly: InsertAfter works
    correctly even while Word has no OS focus at all - the pause is a
    deliberate feature carried over from this app's Notepad days, not
    something Word automation actually requires.)"""

    def __init__(self):
        self.running_event = threading.Event()  # set = not paused
        self.running_event.set()
        self.stop_flag = threading.Event()
        self.thread = None

    def start(
        self, tokens, wpm, on_status, on_progress, on_done, file_path, font_name, font_size, needs_separator,
        board=None, board_needs_separator=False,
    ):
        """file_path=None means no Word output (web-board only); board=None
        means no board output. At least one of the two should be given."""
        self.stop_flag.clear()
        self.running_event.set()
        self.thread = threading.Thread(
            target=self._run,
            args=(
                tokens, wpm, on_status, on_progress, on_done, file_path, font_name, font_size, needs_separator,
                board, board_needs_separator,
            ),
            daemon=True,
        )
        self.thread.start()

    def pause(self):
        self.running_event.clear()

    def resume(self):
        self.running_event.set()

    def stop(self):
        self.stop_flag.set()
        self.running_event.set()  # unblock a paused wait so it can see the stop flag

    def stop_and_wait(self, timeout=2.0):
        self.stop()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=timeout)

    def is_running(self):
        return bool(self.thread and self.thread.is_alive())

    def _run(
        self, tokens, wpm, on_status, on_progress, on_done, file_path, font_name, font_size, needs_separator,
        board=None, board_needs_separator=False,
    ):
        # COM objects have thread affinity (the STA apartment model) -
        # every thread that touches Word must initialize COM itself, the
        # same per-thread requirement UI Automation had for Notepad.
        pythoncom.CoInitialize()
        completed = False
        try:
            completed = bool(
                self._run_inner(
                    tokens, wpm, on_status, on_progress, on_done, file_path, font_name, font_size, needs_separator,
                    board, board_needs_separator,
                )
            )
        finally:
            if board is not None:
                board.set_status("done" if completed else "stopped")
            pythoncom.CoUninitialize()

    def _run_inner(
        self, tokens, wpm, on_status, on_progress, on_done, file_path, font_name, font_size, needs_separator,
        board=None, board_needs_separator=False,
    ):
        seconds_per_word = 60.0 / wpm
        # Enough words per call to roughly keep pace with the requested
        # WPM without calling more often than WORD_CALL_INTERVAL_SECONDS
        # supports - in practice this comes out to 1 word per call for
        # any reasonable WPM, since Word's COM calls are cheap.
        words_per_batch = max(1, round(WORD_CALL_INTERVAL_SECONDS / seconds_per_word))
        total_words = sum(1 for is_word, _ in tokens if is_word)

        use_word = file_path is not None
        doc = None
        target_hwnd = None
        if use_word:
            doc = get_word_document(file_path)
            if doc is None:
                on_status("Could not open the Word document.")
                return
            apply_word_font(doc, font_name, font_size)
            try:
                target_hwnd = doc.ActiveWindow.Hwnd
            except Exception:
                on_status("Could not find the Word window.")
                return

            if needs_separator:
                try:
                    doc.Content.InsertAfter(ENTRY_SEPARATOR)
                except Exception as exc:
                    on_status(f"Could not write to Word: {exc}")
                    return

        if board is not None:
            if board_needs_separator:
                board.append(ENTRY_SEPARATOR)
            board.set_status("typing", 0, total_words)

        # Unlike the old Notepad version, which had to re-send the WHOLE
        # document on every flush (SetValue() replaces content rather
        # than appending), Word's InsertAfter() genuinely appends - so
        # pending_chunks only ever holds what's new since the last flush,
        # cleared after each one.
        pending_chunks = []
        done_words = 0
        words_since_flush = 0

        def flush():
            text = "".join(pending_chunks)
            if use_word:
                if not win32gui.IsWindow(target_hwnd):
                    on_status("Word window closed - stopped.")
                    return False
                try:
                    doc.Content.InsertAfter(text)
                    scroll_word_to_end(doc)
                except Exception as exc:
                    on_status(f"Could not write to Word: {exc}")
                    return False
            if board is not None:
                board.append(text)
                board.set_status("typing", done_words, total_words)
            pending_chunks.clear()
            on_progress(done_words, total_words)
            return True

        for is_word, chunk in tokens:
            if not self._wait_until_ready(target_hwnd, on_status, board):
                return  # stopped, or window closed - status already set

            pending_chunks.append(chunk)
            if is_word:
                done_words += 1
                words_since_flush += 1

            if words_since_flush >= words_per_batch:
                call_start = time.time()
                if not flush():
                    return
                words_since_flush = 0
                target_duration = seconds_per_word * words_per_batch
                # stop_flag.wait() instead of time.sleep(): at slow WPM this
                # pacing delay can be several seconds, long enough that a
                # plain sleep would make stop() take just as long to take
                # effect - which matters since a new remote-triggered job
                # stops whatever's currently typing (see remote_watch_loop)
                # rather than waiting for it to finish. wait() returns the
                # instant stop() sets the flag, so the next loop
                # iteration's _wait_until_ready check (which reports
                # "Stopped") fires almost immediately instead of up to one
                # whole batch-interval late.
                self.stop_flag.wait(max(0, target_duration - (time.time() - call_start)))

        if pending_chunks:
            if not flush():  # final flush for any remaining tail (last partial batch, trailing whitespace)
                return
        elif use_word:
            scroll_word_to_end(doc)  # nothing left to flush, but make sure the view is fully caught up
        on_status("Done")
        on_done()
        return True

    def _wait_until_ready(self, target_hwnd, on_status, board=None):
        """Blocks until not manually paused AND Word has OS focus, so
        switching away pauses generation immediately (nothing more gets
        written) and switching back resumes it exactly where it left off -
        this is what lets the user freely type in another app (WhatsApp,
        Teams, ...) without the background writes interfering there.
        Checked once per token (word or whitespace run), not per-character -
        since writing now happens as atomic COM calls rather than
        individual keystrokes, there's no risk of a corrupted partial word
        from pausing here, so this doesn't need to be any finer-grained
        than that. Returns False (with on_status already set) if stop()
        was called or the target window has been closed.

        target_hwnd=None (web-board-only output) has no Word window to
        watch, so only the manual pause and stop apply. With Word AND the
        board, a foreground window titled "Answer Board" also counts as
        focused - otherwise watching the board in a browser would leave
        Word unfocused and pause the very output being watched."""
        announced = False
        while True:
            if board is not None and not self.running_event.is_set():
                board.set_status("paused")  # manual pause - the board page mirrors it
            self.running_event.wait()
            if self.stop_flag.is_set():
                on_status("Stopped")
                return False
            if target_hwnd is None:
                return True
            if not win32gui.IsWindow(target_hwnd):
                on_status("Word window closed - stopped.")
                return False
            try:
                foreground = win32gui.GetForegroundWindow()
                has_focus = foreground == target_hwnd or (
                    board is not None and BOARD_WINDOW_TITLE in win32gui.GetWindowText(foreground)
                )
            except Exception:
                has_focus = True  # can't tell - don't get stuck waiting forever
            if has_focus:
                return True
            if not announced:
                on_status("Paused - switch back to Word to resume typing.")
                if board is not None:
                    board.set_status("paused")
                announced = True
            time.sleep(0.15)


def force_foreground(hwnd):
    """Brings a window to the foreground, working around Windows' restriction
    on background processes stealing focus. Attaches this thread's input
    state to both the current foreground window's thread and the target
    window's thread - Windows allows SetForegroundWindow without restriction
    between attached threads. An earlier version simulated an Alt keypress to
    get the same effect, but that risks leaving Alt "stuck" if its key-up
    doesn't land cleanly, corrupting later keystrokes into menu shortcuts
    (e.g. typed text triggering Insert>Table, or a hotkey acting like a
    different, unintended shortcut). This approach injects no keyboard
    events at all."""
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

    current_thread = win32api.GetCurrentThreadId()
    target_thread, _ = win32process.GetWindowThreadProcessId(hwnd)
    fg_hwnd = win32gui.GetForegroundWindow()
    fg_thread, _ = win32process.GetWindowThreadProcessId(fg_hwnd) if fg_hwnd else (0, 0)

    attached_target = attached_fg = False
    try:
        if target_thread and target_thread != current_thread:
            win32process.AttachThreadInput(current_thread, target_thread, True)
            attached_target = True
        if fg_thread and fg_thread != current_thread and fg_thread != target_thread:
            win32process.AttachThreadInput(current_thread, fg_thread, True)
            attached_fg = True
        win32gui.SetForegroundWindow(hwnd)
    finally:
        if attached_target:
            win32process.AttachThreadInput(current_thread, target_thread, False)
        if attached_fg:
            win32process.AttachThreadInput(current_thread, fg_thread, False)


def create_tray_icon_image():
    """Generates the system tray icon in memory - no bundled asset file
    needed. Just a simple, distinctive colored square, not meant to be
    anything elaborate."""
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([4, 4, size - 4, size - 4], radius=12, fill=(37, 99, 235, 255))
    draw.rounded_rectangle([20, 20, size - 20, size - 20], radius=6, fill=(255, 255, 255, 255))
    return image


class App:
    def __init__(self, root):
        self.root = root
        root.title("Clipboard Auto Typer")
        root.geometry("600x400")

        # This laptop plays one of two roles at a time - "type" (types
        # copied/relayed text into a local Word document) or "send" (relays
        # its own clipboard to another laptop that's in "type" mode). Only
        # one role's controls are relevant at once, so the mode switch below
        # shows/hides them instead of leaving every control visible
        # regardless of which role this machine is actually playing.
        mode_frame = tk.Frame(root)
        mode_frame.pack(fill="x", padx=10, pady=(12, 6))
        tk.Label(mode_frame, text="This laptop:").pack(side="left")
        self.mode_var = tk.StringVar(value="type")
        tk.Radiobutton(
            mode_frame, text="Types (into Word)", variable=self.mode_var, value="type", command=self.on_mode_change
        ).pack(side="left", padx=(6, 0))
        tk.Radiobutton(
            mode_frame, text="Sends (to another laptop)", variable=self.mode_var, value="send", command=self.on_mode_change
        ).pack(side="left", padx=(6, 0))

        # Internet relay (ntfy.sh) works from anywhere but depends on a
        # third party and its daily message quota; direct LAN connection
        # only works when both laptops share a network, but then it's
        # faster and has no quota at all. Applies regardless of which
        # role (above) this laptop is playing.
        connection_frame = tk.Frame(root)
        connection_frame.pack(fill="x", padx=10, pady=(0, 6))
        tk.Label(connection_frame, text="Connection:").pack(side="left")
        self.connection_type_var = tk.StringVar(value="internet")
        tk.Radiobutton(
            connection_frame, text="Internet Relay", variable=self.connection_type_var, value="internet",
            command=self.on_connection_type_change,
        ).pack(side="left", padx=(6, 0))
        tk.Radiobutton(
            connection_frame, text="Local Network (LAN)", variable=self.connection_type_var, value="lan",
            command=self.on_connection_type_change,
        ).pack(side="left", padx=(6, 0))

        # ---- "type" mode controls ----
        self.type_container = tk.Frame(root)

        path_frame = tk.Frame(self.type_container)
        path_frame.pack(fill="x", padx=10, pady=(0, 6))
        tk.Label(path_frame, text="Word document:").pack(side="left")
        self.file_path_var = tk.StringVar()
        self.file_path_entry = tk.Entry(path_frame, textvariable=self.file_path_var)
        self.file_path_entry.pack(side="left", fill="x", expand=True, padx=(6, 6))
        self.browse_btn = tk.Button(path_frame, text="Browse...", command=self.browse_file)
        self.browse_btn.pack(side="left")

        output_frame = tk.Frame(self.type_container)
        output_frame.pack(fill="x", padx=10, pady=(0, 6))
        tk.Label(output_frame, text="Output:").pack(side="left")
        self.output_var = tk.StringVar(value="word")
        for label, value in (("Word", "word"), ("Web page", "web"), ("Both", "both")):
            tk.Radiobutton(
                output_frame, text=label, variable=self.output_var, value=value, command=self.on_output_change
            ).pack(side="left", padx=(6, 0))
        self.open_board_btn = tk.Button(
            output_frame, text="Open Answer Board", command=self.open_board, state="disabled"
        )
        self.open_board_btn.pack(side="left", padx=(12, 0))

        font_frame = tk.Frame(self.type_container)
        font_frame.pack(fill="x", padx=10, pady=(0, 6))
        tk.Label(font_frame, text="Font:").pack(side="left")
        self.font_name_var = tk.StringVar(value=WORD_DEFAULT_FONT_NAME)
        tk.Entry(font_frame, textvariable=self.font_name_var, width=16).pack(side="left", padx=(6, 12))
        tk.Label(font_frame, text="Size:").pack(side="left")
        self.font_size_var = tk.IntVar(value=WORD_DEFAULT_FONT_SIZE)
        tk.Spinbox(font_frame, from_=6, to=96, textvariable=self.font_size_var, width=4).pack(side="left", padx=(6, 0))

        control_frame = tk.Frame(self.type_container)
        control_frame.pack(fill="x", padx=10, pady=(0, 6))

        tk.Label(control_frame, text="Speed (WPM):").pack(side="left")
        self.wpm_var = tk.IntVar(value=80)
        tk.Scale(
            control_frame, from_=20, to=300, orient="horizontal", variable=self.wpm_var, length=200
        ).pack(side="left", padx=(4, 20))

        self.pause_btn = tk.Button(control_frame, text="Pause", width=10, state="disabled", command=self.on_pause)
        self.pause_btn.pack(side="left", padx=4)
        self.stop_btn = tk.Button(control_frame, text="Stop", width=10, state="disabled", command=self.on_stop)
        self.stop_btn.pack(side="left", padx=4)

        watch_frame = tk.Frame(self.type_container)
        watch_frame.pack(fill="x", padx=10, pady=(0, 6))
        self.watch_btn = tk.Button(watch_frame, text="Enable Clipboard Auto-Type", command=self.toggle_watch)
        self.watch_btn.pack(side="left")

        remote_frame = tk.Frame(self.type_container)
        remote_frame.pack(fill="x", padx=10, pady=(0, 2))
        self.remote_btn = tk.Button(remote_frame, text="Enable Web Remote Trigger", command=self.toggle_remote_watch)
        self.remote_btn.pack(side="left")
        self.remote_status_label = tk.Label(self.type_container, text="Remote trigger: Off", fg="gray", anchor="w")
        self.remote_status_label.pack(fill="x", padx=10, pady=(0, 6))

        tk.Label(self.type_container, text=f"Pause/Resume hotkey: {PAUSE_HOTKEY} (works from any app)", fg="gray").pack(
            anchor="w", padx=10, pady=(0, 4)
        )

        self.type_container.pack(fill="x")

        # ---- "send" mode controls ----
        self.send_container = tk.Frame(root)

        # Only relevant/shown for LAN connections - the internet relay
        # doesn't need this since ntfy.sh's topic already identifies where
        # to send. Packed before send_remote_frame so it appears above the
        # button when shown; visibility is toggled by
        # on_connection_type_change, not by mode alone.
        self.lan_ip_frame = tk.Frame(self.send_container)
        tk.Label(self.lan_ip_frame, text="Receiver's LAN IP:").pack(side="left")
        self.lan_receiver_ip_var = tk.StringVar()
        tk.Entry(self.lan_ip_frame, textvariable=self.lan_receiver_ip_var, width=16).pack(side="left", padx=(6, 0))

        send_remote_frame = tk.Frame(self.send_container)
        self._send_remote_frame = send_remote_frame  # referenced by on_connection_type_change for pack ordering
        send_remote_frame.pack(fill="x", padx=10, pady=(0, 2))
        self.send_remote_btn = tk.Button(
            send_remote_frame, text="Send Clipboard to Remote Laptop", command=self.toggle_send_clipboard_remote
        )
        self.send_remote_btn.pack(side="left")
        # Fires one heartbeat immediately instead of waiting up to
        # HEARTBEAT_INTERVAL_SECONDS for the next automatic one, and
        # reports the outcome of that specific network call directly -
        # useful when actively troubleshooting rather than watching the
        # passive Relay: status settle on its own timer.
        self.test_connection_btn = tk.Button(send_remote_frame, text="Test Connection", command=self.test_connection)
        self.test_connection_btn.pack(side="left", padx=(6, 0))
        self.relay_status_label = tk.Label(self.send_container, text="Relay: Off", fg="gray", anchor="w")
        self.relay_status_label.pack(fill="x", padx=10, pady=(0, 6))

        self.status_label = tk.Label(root, text="Idle", anchor="w")
        self.status_label.pack(side="bottom", fill="x", padx=10, pady=(0, 10))

        self.typer = AutoTyper()
        self.paused = False
        self.board = BoardHub()
        self.board_server = None
        self.board_stop_thread = None
        self.board_lock = threading.Lock()

        self.clipboard_watch_enabled = False
        self.last_clipboard_text = ""
        self.word_hwnd = None

        self.remote_config = load_remote_config()
        self.remote_watch_enabled = False
        if self.remote_config is None:
            self.remote_btn.config(state="disabled")
            self.send_remote_btn.config(state="disabled")
        else:
            self.lan_receiver_ip_var.set(self.remote_config.get("lan_receiver_ip", ""))

        self.send_clipboard_remote_enabled = False
        self.last_sent_clipboard_text = ""
        # Send mode's best-known view of the receiving laptop's actual
        # pause state - updated from the receiver's response on every LAN
        # heartbeat/text POST, or from the receiver's own periodic
        # heartbeat broadcast over Internet Relay (see remote_set_paused,
        # heartbeat_sender_loop, relay_health_loop). Used so the Ctrl+Alt+P
        # hotkey on THIS laptop knows whether to send "pause" or "resume"
        # next, even if the receiving laptop was also toggled locally.
        self.remote_typer_paused = False

        # Heartbeat/pairing state - separate per role so the idle role's
        # loop (always running, just gated off) can't clobber the active
        # role's state by both writing the same shared variable. Also
        # used for LAN mode's "Connected" status (see
        # connection_status_ticker) even though LAN doesn't hold a
        # persistent connection the way the ntfy.sh SSE subscription does.
        self.type_relay_link_up = False
        self.type_last_peer_heartbeat = 0.0
        self.type_last_relay_error = None
        self.send_relay_link_up = False
        self.send_last_peer_heartbeat = 0.0
        self.send_last_relay_error = None

        # LAN-specific state. lan_server/lan_local_ip only exist while the
        # LAN listener is actually running; lan_last_request_time is
        # updated by LANRequestHandler whenever anything (heartbeat or
        # text) arrives from a sending laptop.
        self.lan_server = None
        self.lan_local_ip = None
        self.lan_last_request_time = 0.0

        self.last_hotkey_time = 0.0

        threading.Thread(target=self.clipboard_watch_loop, daemon=True).start()
        threading.Thread(target=self.remote_watch_loop, daemon=True).start()
        threading.Thread(target=self.clipboard_to_remote_loop, daemon=True).start()
        threading.Thread(target=self.relay_health_loop, daemon=True).start()
        threading.Thread(target=self.heartbeat_sender_loop, daemon=True).start()
        threading.Thread(target=self.connection_status_ticker, daemon=True).start()
        threading.Thread(target=self.autosave_settings_loop, daemon=True).start()
        keyboard.add_hotkey(PAUSE_HOTKEY, self.on_pause_hotkey, suppress=True)

        self.apply_saved_settings(load_app_settings())

        # Closing the window (X) minimizes to tray instead of quitting -
        # this app is meant to run continuously in the background, not be
        # closed and reopened. Only the tray menu's "Exit" actually ends
        # the process.
        self.tray_icon = None
        root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        threading.Thread(target=self.run_tray_icon, daemon=True).start()

    # ---- system tray ----

    def hide_to_tray(self):
        self.root.withdraw()

    def show_window(self, icon=None, item=None):
        # pystray menu callbacks run on the tray icon's own thread, not
        # Tkinter's - same reasoning as every other cross-thread UI update
        # in this app (on_status, set_remote_status, ...).
        self.root.after(0, self._do_show_window)

    def _do_show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def exit_app(self, icon=None, item=None):
        self.stop_remote_watch()  # closes the LAN server socket cleanly, if one was open
        if self.tray_icon is not None:
            self.tray_icon.stop()
        self.root.after(0, self.root.destroy)

    def run_tray_icon(self):
        menu = pystray.Menu(
            pystray.MenuItem("Show Window", self.show_window, default=True),
            pystray.MenuItem("Exit", self.exit_app),
        )
        self.tray_icon = pystray.Icon("clipboard_auto_typer", create_tray_icon_image(), "Clipboard Auto Typer", menu)
        self.tray_icon.run()  # blocks this thread until self.tray_icon.stop() is called

    # ---- settings persistence ----

    def apply_saved_settings(self, settings):
        """Restores last-used mode/connection type/Word document path/
        speed/font, and re-enables whichever toggle was on when last
        saved, so a restart doesn't require re-selecting everything - see
        autosave_settings_loop for where these get saved. Called once at
        startup with whatever load_app_settings() found (an empty dict,
        and every setting left at its widget default, if this is the
        first run or the file's missing/corrupt)."""
        mode = settings.get("mode")
        if mode in ("type", "send"):
            self.mode_var.set(mode)
        connection_type = settings.get("connection_type")
        if connection_type in ("internet", "lan"):
            self.connection_type_var.set(connection_type)
        file_path = settings.get("file_path")
        if file_path:
            self.file_path_var.set(file_path)
        wpm = settings.get("wpm")
        if isinstance(wpm, int) and 20 <= wpm <= 300:
            self.wpm_var.set(wpm)
        font_name = settings.get("font_name")
        if font_name:
            self.font_name_var.set(font_name)
        font_size = settings.get("font_size")
        if isinstance(font_size, int) and 6 <= font_size <= 96:
            self.font_size_var.set(font_size)
        output = settings.get("output")
        if output in ("word", "web", "both"):
            self.output_var.set(output)
            self.on_output_change()

        # Same handlers a user clicking the radio buttons would trigger -
        # syncs container visibility etc. Safe to call here since nothing
        # is enabled yet, so their "stop whatever was running" step is a
        # harmless no-op.
        self.on_mode_change()
        self.on_connection_type_change()

        if self.remote_config is None:
            return
        if self.mode_var.get() == "type" and settings.get("remote_watch_enabled"):
            self.toggle_remote_watch()
        elif self.mode_var.get() == "send" and settings.get("send_clipboard_remote_enabled"):
            self.toggle_send_clipboard_remote()

    def autosave_settings_loop(self):
        """Periodically snapshots current mode/connection/path/speed/font/
        toggle state to app_settings.json (rather than wiring a trace on
        every relevant widget variable), and piggybacks the crash-log
        "still alive" heartbeat on the same timer - see log_still_alive
        for why that matters even though this loop isn't itself doing
        anything related to crash detection."""
        last_heartbeat_log = 0.0
        while True:
            time.sleep(5)
            save_app_settings(
                {
                    "mode": self.mode_var.get(),
                    "connection_type": self.connection_type_var.get(),
                    "file_path": self.file_path_var.get(),
                    "wpm": self.wpm_var.get(),
                    "font_name": self.font_name_var.get(),
                    "font_size": self.font_size_var.get(),
                    "output": self.output_var.get(),
                    "remote_watch_enabled": self.remote_watch_enabled,
                    "send_clipboard_remote_enabled": self.send_clipboard_remote_enabled,
                }
            )
            now = time.time()
            if now - last_heartbeat_log >= 60:
                log_still_alive()
                last_heartbeat_log = now

    # ---- mode switch ----

    def on_mode_change(self):
        mode = self.mode_var.get()
        if mode == "type":
            self.stop_send_clipboard_remote()
            self.send_container.pack_forget()
            self.type_container.pack(fill="x")
            self.status_label.config(text="Idle")
        else:
            # Leaving "type" mode - stop anything that would otherwise keep
            # running invisibly with its controls hidden.
            self.typer.stop()
            self.reset_button_states()
            if self.clipboard_watch_enabled:
                self.clipboard_watch_enabled = False
                self.watch_btn.config(text="Enable Clipboard Auto-Type")
                self.file_path_entry.config(state="normal")
                self.browse_btn.config(state="normal")
            self.stop_remote_watch()
            self.type_container.pack_forget()
            self.send_container.pack(fill="x")
            self.status_label.config(text="Idle")

    def on_connection_type_change(self):
        # Switching transport mid-flight would leave stale state from the
        # old one (an SSE thread still trying to reach ntfy.sh right after
        # LAN was selected, or vice versa) - stop whichever role is
        # currently active so re-enabling it starts fresh on the newly
        # selected transport.
        self.stop_remote_watch()
        self.stop_send_clipboard_remote()
        if self.connection_type_var.get() == "lan":
            self.lan_ip_frame.pack(fill="x", padx=10, pady=(0, 6), before=self._send_remote_frame)
        else:
            self.lan_ip_frame.pack_forget()
        self.status_label.config(text="Idle")

    def stop_remote_watch(self):
        """Turns off the "type" mode remote trigger, whichever transport
        it's using, and cleans up the LAN server if one was running -
        shared by on_mode_change, on_connection_type_change, and
        toggle_remote_watch's own off-switch path."""
        if not self.remote_watch_enabled:
            return
        self.remote_watch_enabled = False
        self.remote_btn.config(text="Enable Web Remote Trigger")
        if self.lan_server is not None:
            self.lan_server.shutdown()
            self.lan_server.server_close()
            self.lan_server = None

    def stop_send_clipboard_remote(self):
        """Turns off "send" mode's clipboard relay - shared by
        on_mode_change, on_connection_type_change, and
        toggle_send_clipboard_remote's own off-switch path."""
        if not self.send_clipboard_remote_enabled:
            return
        self.send_clipboard_remote_enabled = False
        self.send_remote_btn.config(text="Send Clipboard to Remote Laptop")

    # ---- pause/stop controls ----

    def on_pause_hotkey(self):
        now = time.time()
        if now - self.last_hotkey_time < HOTKEY_DEBOUNCE_SECONDS:
            return  # ignore OS key-repeat firing this multiple times per press
        self.last_hotkey_time = now
        self.root.after(0, self.handle_pause_hotkey)

    def handle_pause_hotkey(self):
        """In Types mode this pauses/resumes the local typing job, same as
        the Pause button. In Sends mode there's no local job to pause - the
        same hotkey instead sends a pause/resume command to the receiving
        laptop over whatever connection is active, so one hotkey controls
        typing from either laptop (see remote_typer_paused for how the
        sender tracks the receiver's real state instead of just guessing)."""
        mode = self.mode_var.get()
        if mode == "type":
            if self.typer.is_running():
                self.on_pause()
        elif mode == "send":
            if self.remote_config is None:
                self.status_label.config(text="Remote trigger not configured - see remote_config.json.")
                return
            if not self.send_clipboard_remote_enabled:
                self.status_label.config(text="Enable 'Send Clipboard to Remote Laptop' first.")
                return
            if self.connection_type_var.get() == "lan" and not self.lan_receiver_ip_var.get().strip():
                self.status_label.config(text="Enter the receiving laptop's LAN IP address first.")
                return
            # Optimistic - corrected by the next heartbeat/response if this
            # guess turns out to be stale (e.g. the receiving laptop was
            # also toggled locally since we last heard from it).
            should_pause = not self.remote_typer_paused
            self.remote_typer_paused = should_pause
            threading.Thread(target=self.send_pause_resume_command, args=(should_pause,), daemon=True).start()

    def send_pause_resume_command(self, should_pause):
        secret = self.remote_config["secret"]
        kind = "pause" if should_pause else "resume"
        payload = json.dumps({"secret": secret, "kind": kind})
        try:
            if self.connection_type_var.get() == "lan":
                ip = self.lan_receiver_ip_var.get().strip()
                port = self.remote_config.get("lan_port", LAN_DEFAULT_PORT)
                resp = requests.post(
                    f"http://{ip}:{port}/", data=payload, headers={"Content-Type": "text/plain"}, timeout=10
                )
                self.update_remote_paused_from_response(resp)
            else:
                topic = self.remote_config["topic"]
                requests.post(
                    f"{NTFY_BASE_URL}/{topic}", data=payload, headers={"Content-Type": "text/plain"}, timeout=10
                )
                # No response body to confirm with over Internet Relay -
                # the receiver's own periodic heartbeat broadcast will
                # correct remote_typer_paused within HEARTBEAT_INTERVAL_SECONDS
                # if this optimistic guess was wrong (see relay_health_loop).
            self.on_status(f"Sent {kind} to remote laptop.")
        except Exception as exc:
            self.on_status(f"Could not send {kind} command: {exc}")

    def update_remote_paused_from_response(self, resp):
        """Reads the receiving laptop's actual current pause state back
        from a LAN response body (see LANRequestHandler._handle_post),
        keeping remote_typer_paused accurate instead of relying purely on
        this laptop's own optimistic guess."""
        try:
            self.remote_typer_paused = bool(resp.json().get("paused", False))
        except (ValueError, AttributeError):
            pass

    def on_pause(self):
        self.set_paused_state(not self.paused)

    def set_paused_state(self, should_pause):
        if should_pause:
            self.typer.pause()
            self.paused = True
            self.pause_btn.config(text="Resume")
            self.status_label.config(text="Paused")
        else:
            self.typer.resume()
            self.paused = False
            self.pause_btn.config(text="Pause")

    def remote_set_paused(self, should_pause):
        """Applies a pause/resume command received from the sending
        laptop (LAN: LANRequestHandler._handle_post; Internet Relay:
        remote_watch_loop) - called via root.after() since both of those
        run on background threads. Mirrors on_pause()'s own guard: no
        point pausing/resuming a job that isn't running."""
        if not self.typer.is_running():
            return
        if should_pause == self.paused:
            return
        self.set_paused_state(should_pause)

    def on_stop(self):
        self.typer.stop()
        self.reset_button_states()

    # ---- clipboard auto-type flow ----

    def browse_file(self):
        path = filedialog.asksaveasfilename(defaultextension=".docx", filetypes=[("Word documents", "*.docx"), ("All files", "*.*")])
        if path:
            self.file_path_var.set(path)

    def toggle_watch(self):
        if not self.clipboard_watch_enabled:
            file_path = self.file_path_var.get().strip()
            if not file_path:
                self.status_label.config(text="Select a Word document path first.")
                return
            self.clipboard_watch_enabled = True
            self.last_clipboard_text = self.safe_paste()
            self.watch_btn.config(text="Disable Clipboard Auto-Type")
            self.file_path_entry.config(state="disabled")
            self.browse_btn.config(state="disabled")
            self.status_label.config(text="Opening Word...")
            # Pre-launch/find Word now instead of waiting for the first
            # copy - that first launch is the slow part (spawning the
            # process and waiting for its window), so do it up front.
            threading.Thread(target=self.prelaunch_word, args=(file_path,), daemon=True).start()
        else:
            self.clipboard_watch_enabled = False
            self.watch_btn.config(text="Enable Clipboard Auto-Type")
            self.file_path_entry.config(state="normal")
            self.browse_btn.config(state="normal")
            self.status_label.config(text="Clipboard watching stopped.")

    def safe_paste(self):
        try:
            return pyperclip.paste()
        except Exception as exc:
            self.on_status(f"Clipboard read error: {exc}")
            return ""

    def clipboard_watch_loop(self):
        # Same reason as in AutoTyper._run - this is a spawned thread, and
        # this thread also makes Word COM calls directly (reading the
        # document's existing content before a new job starts), so it
        # needs its own COM init too.
        pythoncom.CoInitialize()
        while True:
            time.sleep(CLIPBOARD_POLL_SECONDS)
            if not self.clipboard_watch_enabled:
                continue
            try:
                current = self.safe_paste()
                if not current or current == self.last_clipboard_text:
                    continue
                if self.typer.is_running():
                    # A job is active (typing or paused waiting for focus).
                    # Don't let a clipboard change - even an accidental one
                    # from another app - abort it mid-way and wipe out what's
                    # already been typed. Deliberately don't update
                    # last_clipboard_text here: this same check re-runs every
                    # poll, so once the current job finishes, whatever is on
                    # the clipboard then (this text, or something newer) gets
                    # picked up automatically.
                    continue
                # Only mark this text as "handled" if we actually managed to
                # start typing it. Marking it handled unconditionally (as
                # before) meant a transient failure - e.g. Word still
                # starting up right after a fresh launch - would silently
                # and PERMANENTLY blacklist that
                # exact clipboard text: it would never be retried, even
                # though the same copy would very likely succeed a second
                # later. Re-copying identical text is indistinguishable from
                # "nothing happening" to the user, so this was showing up as
                # the app having just stopped working.
                if self.handle_new_clipboard_text(current):
                    self.last_clipboard_text = current
            except Exception as exc:
                # Without this, any error here (a Win32 call failing, etc.)
                # would silently kill this background thread forever - the
                # UI would keep saying "Watching..." while doing nothing.
                self.on_status(f"Clipboard watcher error: {exc}")

    def handle_new_clipboard_text(self, text):
        """Attempts to start typing `text` into the target Word document, at
        the speed slider's current setting - remote-triggered jobs use this
        same speed, not a separate fixed pace. Appends after whatever's
        already there (separated by ENTRY_SEPARATOR) rather than clearing it
        first - if a prior job was interrupted mid-sentence (see
        remote_watch_loop), that partial text is left exactly as it was, not
        wiped. Returns True only if a typing job was actually started - the
        caller uses this to decide whether this clipboard content may be
        safely considered "handled", so a transient failure gets retried on
        the next poll instead of being silently and permanently ignored."""
        tokens = tokenize(text)
        if not any(is_word for is_word, _ in tokens):
            return True  # nothing to type, but not a failure - don't retry it

        output = self.output_var.get()
        use_word = output in ("word", "both")
        use_board = output in ("web", "both")

        file_path = self.file_path_var.get().strip()
        if use_word and not file_path:
            self.on_status("Set a Word document path first, then copy again.")
            return False

        self.typer.stop_and_wait()

        needs_separator = False
        if use_word:
            if not self.ensure_word_open(file_path):
                return False

            doc = get_word_document(file_path)
            if doc is None:
                self.on_status("Could not open the Word document - will retry.")
                return False
            try:
                needs_separator = bool(doc.Content.Text.strip())
            except Exception as exc:
                self.on_status(f"Could not read the Word document's current text: {exc} - will retry.")
                return False

            try:
                force_foreground(self.word_hwnd)
            except Exception:
                pass  # best-effort only - typing doesn't depend on this succeeding

        board_needs_separator = False
        if use_board:
            if not self.start_board_server():
                return False
            board_needs_separator = self.board.has_text()

        self.root.after(0, self.set_controls_running)
        self.typer.start(
            tokens,
            self.wpm_var.get(),
            self.on_status,
            self.on_progress,
            self.on_done_auto,
            file_path if use_word else None,
            self.font_name_var.get().strip() or WORD_DEFAULT_FONT_NAME,
            self.font_size_var.get(),
            needs_separator,
            board=self.board if use_board else None,
            board_needs_separator=board_needs_separator,
        )
        return True

    # ---- Answer Board (local web page output) ----

    def start_board_server(self):
        """Starts the localhost-only Answer Board server if it isn't
        already running. Safe to call from any thread (the typing paths
        call it from background threads, the Output setting from Tk)."""
        with self.board_lock:
            if self.board_server is not None:
                return True
            stopping = self.board_stop_thread
            if stopping is not None:
                stopping.join(timeout=3)  # a just-stopped server still holds the port until it finishes closing
            try:
                server = BoardServer(("127.0.0.1", BOARD_PORT), BoardRequestHandler)
            except OSError as exc:
                self.on_status(f"Could not start the Answer Board on port {BOARD_PORT}: {exc}")
                return False
            server.hub = self.board
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.board_server = server
            return True

    def stop_board_server(self):
        with self.board_lock:
            server, self.board_server = self.board_server, None
            if server is None:
                return
            server.stopping.set()  # ends any open page streams at their next keepalive
            # shutdown() waits for serve_forever() to exit, so it can't run
            # on the thread that would be blocked by it. start_board_server
            # waits on this thread before rebinding the port.
            self.board_stop_thread = threading.Thread(
                target=lambda: (server.shutdown(), server.server_close()), daemon=True
            )
            self.board_stop_thread.start()

    def on_output_change(self):
        if self.output_var.get() in ("web", "both"):
            if self.start_board_server():
                self.open_board_btn.config(state="normal")
                self.status_label.config(text=f"Answer Board ready: {BOARD_URL}")
            else:
                self.output_var.set("word")
                self.open_board_btn.config(state="disabled")
        else:
            self.stop_board_server()
            self.open_board_btn.config(state="disabled")

    def open_board(self):
        webbrowser.open(BOARD_URL)

    # ---- web remote trigger flow ----

    def toggle_remote_watch(self):
        if self.remote_config is None:
            self.status_label.config(text="Remote trigger not configured - see remote_config.json.")
            return
        if self.remote_watch_enabled:
            self.stop_remote_watch()
            self.status_label.config(text="Remote trigger stopped.")
            return
        output = self.output_var.get()
        use_word = output in ("word", "both")
        file_path = self.file_path_var.get().strip()
        if use_word and not file_path:
            self.status_label.config(text="Select a Word document path first.")
            return
        if output in ("web", "both") and not self.start_board_server():
            return
        if self.connection_type_var.get() == "lan":
            port = self.remote_config.get("lan_port", LAN_DEFAULT_PORT)
            try:
                server = LANServer(("0.0.0.0", port), LANRequestHandler)
            except OSError as exc:
                self.status_label.config(text=f"Could not start LAN listener on port {port}: {exc}")
                return
            server.app_ref = self
            self.lan_server = server
            self.lan_local_ip = get_local_ip()
            self.lan_last_request_time = 0.0
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.status_label.config(text=f"LAN listener started on {self.lan_local_ip}:{port}.")
        else:
            self.status_label.config(text="Connecting to relay...")
        self.remote_watch_enabled = True
        # Reset so a stale heartbeat from before this was last enabled
        # can't make the status say "Connected" before a fresh one has
        # actually been seen.
        self.type_last_peer_heartbeat = 0.0
        self.remote_btn.config(text="Disable Web Remote Trigger")
        if use_word:
            threading.Thread(target=self.prelaunch_word, args=(file_path,), daemon=True).start()
        else:
            self.status_label.config(text=f"Waiting for answers - Answer Board: {BOARD_URL}")

    def remote_watch_loop(self):
        # Same reason as clipboard_watch_loop/AutoTyper._run - this thread
        # makes Word COM calls (via handle_new_clipboard_text), so it needs
        # its own COM init too.
        pythoncom.CoInitialize()
        backoff = 1.0
        while True:
            # LAN mode doesn't use this SSE-based loop at all - incoming
            # messages there arrive via LANRequestHandler on the HTTP
            # server thread instead (see toggle_remote_watch).
            if not self.remote_watch_enabled or self.remote_config is None or self.connection_type_var.get() == "lan":
                self.type_relay_link_up = False
                time.sleep(0.5)
                continue
            topic = self.remote_config["topic"]
            secret = self.remote_config["secret"]
            url = f"{NTFY_BASE_URL}/{topic}/sse"
            try:
                # (connect_timeout, read_timeout) - ntfy sends a keepalive
                # roughly every 45s, so a long read timeout would otherwise
                # look identical to a genuinely dead connection.
                with requests.get(url, stream=True, timeout=(10, 90)) as resp:
                    resp.raise_for_status()
                    backoff = 1.0
                    self.type_relay_link_up = True
                    self.type_last_relay_error = None
                    for line in resp.iter_lines(decode_unicode=True):
                        if not self.remote_watch_enabled:
                            break
                        if not line or not line.startswith("data:"):
                            continue
                        try:
                            event = json.loads(line[len("data:"):].strip())
                        except ValueError:
                            continue
                        if event.get("event") != "message":
                            continue
                        try:
                            payload = json.loads(event.get("message", ""))
                        except ValueError:
                            continue
                        # Guards against anyone else who finds/guesses the
                        # topic name - only messages carrying the matching
                        # shared secret are ever acted on.
                        if payload.get("secret") != secret:
                            continue
                        kind = payload.get("kind", "text")
                        if kind == "heartbeat":
                            if payload.get("role") == "send":
                                self.type_last_peer_heartbeat = time.time()
                            continue
                        if kind in ("pause", "resume"):
                            should_pause = kind == "pause"
                            self.root.after(0, lambda sp=should_pause: self.remote_set_paused(sp))
                            continue
                        if kind != "text":
                            continue
                        text = payload.get("text", "")
                        if not text:
                            continue
                        # Unlike the local clipboard flow, a new remote copy
                        # is meant to immediately replace whatever's
                        # currently typing rather than wait for it to
                        # finish - handle_new_clipboard_text already calls
                        # typer.stop_and_wait() before starting, so this
                        # just lets that happen instead of skipping.
                        self.handle_new_clipboard_text(text)
            except Exception as exc:
                self.type_relay_link_up = False
                self.type_last_relay_error = str(exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    # ---- send-clipboard-to-remote-laptop flow ----
    # This is the mirror image of remote_watch_loop: instead of THIS laptop
    # receiving relayed text, THIS laptop watches its own clipboard and
    # auto-relays new copies to the same ntfy topic, so another laptop
    # running this same app with "Enable Web Remote Trigger" on picks it up
    # and types it automatically. Both laptops must share the same
    # remote_config.json (topic + secret); copy that file over once when
    # setting this up.

    def toggle_send_clipboard_remote(self):
        if self.remote_config is None:
            self.status_label.config(text="Remote trigger not configured - see remote_config.json.")
            return
        if self.send_clipboard_remote_enabled:
            self.stop_send_clipboard_remote()
            self.status_label.config(text="Stopped sending clipboard to remote laptop.")
            return
        if self.connection_type_var.get() == "lan":
            ip = self.lan_receiver_ip_var.get().strip()
            if not ip:
                self.status_label.config(text="Enter the receiving laptop's LAN IP address first.")
                return
            self.remote_config["lan_receiver_ip"] = ip
            save_remote_config(self.remote_config)
        self.send_clipboard_remote_enabled = True
        self.send_remote_btn.config(text="Stop Sending Clipboard to Remote Laptop")
        self.last_sent_clipboard_text = self.safe_paste()
        # Same reasoning as toggle_remote_watch - don't let a stale
        # heartbeat from a previous session claim "Connected" early.
        self.send_last_peer_heartbeat = 0.0
        self.status_label.config(text="Sending clipboard copies to the remote laptop...")

    def test_connection(self):
        """Fires one heartbeat-shaped request immediately, instead of
        waiting up to HEARTBEAT_INTERVAL_SECONDS for the next automatic
        one, and reports the outcome of that specific call - lets you
        check "is my own outbound path even working" on demand rather
        than watching the passive Relay: status settle on its own timer.
        Doesn't require Send Clipboard to Remote Laptop to be on."""
        if self.remote_config is None:
            self.status_label.config(text="Remote trigger not configured - see remote_config.json.")
            return
        if self.connection_type_var.get() == "lan" and not self.lan_receiver_ip_var.get().strip():
            self.status_label.config(text="Enter the receiving laptop's LAN IP address first.")
            return
        self.status_label.config(text="Testing connection...")
        threading.Thread(target=self._test_connection_worker, daemon=True).start()

    def _test_connection_worker(self):
        secret = self.remote_config["secret"]
        payload = json.dumps({"secret": secret, "kind": "heartbeat", "role": self.mode_var.get()})
        try:
            if self.connection_type_var.get() == "lan":
                ip = self.lan_receiver_ip_var.get().strip()
                port = self.remote_config.get("lan_port", LAN_DEFAULT_PORT)
                resp = requests.post(
                    f"http://{ip}:{port}/", data=payload, headers={"Content-Type": "text/plain"}, timeout=5
                )
                resp.raise_for_status()
                self.send_last_peer_heartbeat = time.time()  # a successful direct POST IS the confirmation
            else:
                topic = self.remote_config["topic"]
                resp = requests.post(
                    f"{NTFY_BASE_URL}/{topic}", data=payload, headers={"Content-Type": "text/plain"}, timeout=8
                )
                resp.raise_for_status()
            self.on_status("Test message sent successfully.")
        except Exception as exc:
            self.on_status(f"Test failed: {exc}")

    def relay_health_loop(self):
        """Sends mode's counterpart to remote_watch_loop: opens the same
        kind of SSE subscription, but only to detect the receiving
        laptop's heartbeat (see heartbeat_sender_loop) - it doesn't act on
        any text, that's not this laptop's job in "send" mode. This is
        what lets "Relay:" reflect genuine two-way pairing instead of just
        "can this laptop reach ntfy.sh at all", which used to say
        Reachable even with no receiving laptop running."""
        backoff = 1.0
        while True:
            # LAN mode doesn't need this at all - each direct POST already
            # gets an HTTP response confirming delivery, so
            # heartbeat_sender_loop can set send_last_peer_heartbeat
            # itself on success without a separate subscription.
            if (
                self.remote_config is None
                or self.mode_var.get() != "send"
                or not self.send_clipboard_remote_enabled
                or self.connection_type_var.get() == "lan"
            ):
                self.send_relay_link_up = False
                time.sleep(0.5)
                continue
            topic = self.remote_config["topic"]
            secret = self.remote_config["secret"]
            url = f"{NTFY_BASE_URL}/{topic}/sse"
            try:
                with requests.get(url, stream=True, timeout=(10, 90)) as resp:
                    resp.raise_for_status()
                    backoff = 1.0
                    self.send_relay_link_up = True
                    self.send_last_relay_error = None
                    for line in resp.iter_lines(decode_unicode=True):
                        if self.mode_var.get() != "send" or not self.send_clipboard_remote_enabled:
                            break
                        if not line or not line.startswith("data:"):
                            continue
                        try:
                            event = json.loads(line[len("data:"):].strip())
                        except ValueError:
                            continue
                        if event.get("event") != "message":
                            continue
                        try:
                            payload = json.loads(event.get("message", ""))
                        except ValueError:
                            continue
                        if payload.get("secret") != secret:
                            continue
                        if payload.get("kind") == "heartbeat" and payload.get("role") == "type":
                            self.send_last_peer_heartbeat = time.time()
                            if "paused" in payload:
                                self.remote_typer_paused = bool(payload.get("paused"))
            except Exception as exc:
                self.send_relay_link_up = False
                self.send_last_relay_error = str(exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def heartbeat_sender_loop(self):
        """Announces this laptop's presence (role="type" or "send",
        whichever is active) every HEARTBEAT_INTERVAL_SECONDS, so the
        other laptop can tell it's genuinely there - see the module-level
        comment above HEARTBEAT_INTERVAL_SECONDS for why this exists.

        Over the internet relay this is a one-way broadcast the other
        side's own SSE subscription picks up. Over LAN there's no
        subscription on either side - a direct POST's HTTP response IS
        the confirmation, so a "type" role has nothing to send (the
        receiving laptop doesn't initiate anything) and a "send" role
        marks itself as seen immediately on a successful POST rather than
        waiting to observe anything back."""
        while True:
            time.sleep(HEARTBEAT_INTERVAL_SECONDS)
            if self.remote_config is None:
                continue
            mode = self.mode_var.get()
            is_lan = self.connection_type_var.get() == "lan"
            payload_dict = {"secret": self.remote_config["secret"], "kind": "heartbeat"}
            if mode == "type" and self.remote_watch_enabled:
                if is_lan:
                    continue  # nothing to send - see docstring
                # Piggybacks this laptop's real pause state onto its own
                # heartbeat broadcast - over Internet Relay this is the
                # only channel the sending laptop has to learn it (there's
                # no per-request response to read, unlike LAN), so
                # send_pause_resume_command's optimistic guess on that side
                # gets corrected here within one heartbeat interval if it
                # was wrong (see relay_health_loop).
                payload_dict["role"] = "type"
                payload_dict["paused"] = self.paused
            elif mode == "send" and self.send_clipboard_remote_enabled:
                payload_dict["role"] = "send"
            else:
                continue
            payload = json.dumps(payload_dict)
            try:
                if is_lan:
                    ip = self.lan_receiver_ip_var.get().strip()
                    if not ip:
                        continue
                    port = self.remote_config.get("lan_port", LAN_DEFAULT_PORT)
                    resp = requests.post(
                        f"http://{ip}:{port}/", data=payload, headers={"Content-Type": "text/plain"}, timeout=5
                    )
                    self.send_last_peer_heartbeat = time.time()  # the successful POST above IS the confirmation
                    self.update_remote_paused_from_response(resp)
                else:
                    topic = self.remote_config["topic"]
                    requests.post(
                        f"{NTFY_BASE_URL}/{topic}", data=payload, headers={"Content-Type": "text/plain"}, timeout=8
                    )
            except Exception:
                pass  # best-effort - a missed heartbeat just delays "Connected" showing again, nothing breaks

    def connection_status_ticker(self):
        """Single place that decides what the "Remote trigger:"/"Relay:"
        labels actually say, based on this laptop's own relay link plus
        whether a heartbeat from the OTHER role has been seen recently -
        recomputed on a timer (not just on events) since "the peer went
        quiet" is a time-based condition, not something either SSE loop
        would otherwise notice on its own."""
        while True:
            time.sleep(1)
            if self.remote_config is None:
                self.set_remote_status("Not configured")
                self.set_relay_status("Not configured")
                continue
            is_lan = self.connection_type_var.get() == "lan"

            if not self.remote_watch_enabled:
                self.set_remote_status("Off")
            elif is_lan:
                port = self.remote_config.get("lan_port", LAN_DEFAULT_PORT)
                if (time.time() - self.lan_last_request_time) < HEARTBEAT_TIMEOUT_SECONDS:
                    self.set_remote_status("Connected")
                else:
                    self.set_remote_status(f"Listening on {self.lan_local_ip}:{port} - waiting for sending laptop...")
            elif not self.type_relay_link_up:
                detail = f" ({self.type_last_relay_error})" if self.type_last_relay_error else ""
                self.set_remote_status(f"Connecting{detail}...")
            elif (time.time() - self.type_last_peer_heartbeat) < HEARTBEAT_TIMEOUT_SECONDS:
                self.set_remote_status("Connected")
            else:
                self.set_remote_status("Waiting for sending laptop...")

            if not self.send_clipboard_remote_enabled:
                self.set_relay_status("Off")
            elif is_lan:
                if (time.time() - self.send_last_peer_heartbeat) < HEARTBEAT_TIMEOUT_SECONDS:
                    self.set_relay_status("Connected")
                else:
                    self.set_relay_status("Trying to reach the receiving laptop...")
            elif not self.send_relay_link_up:
                detail = f" ({self.send_last_relay_error})" if self.send_last_relay_error else ""
                self.set_relay_status(f"Connecting{detail}...")
            elif (time.time() - self.send_last_peer_heartbeat) < HEARTBEAT_TIMEOUT_SECONDS:
                self.set_relay_status("Connected")
            else:
                self.set_relay_status("Waiting for receiving laptop...")

    def clipboard_to_remote_loop(self):
        while True:
            time.sleep(CLIPBOARD_POLL_SECONDS)
            if not self.send_clipboard_remote_enabled or self.remote_config is None:
                continue
            try:
                current = self.safe_paste()
                if not current or current == self.last_sent_clipboard_text:
                    continue
                secret = self.remote_config["secret"]
                payload = json.dumps({"secret": secret, "kind": "text", "text": current})
                if self.connection_type_var.get() == "lan":
                    ip = self.lan_receiver_ip_var.get().strip()
                    if not ip:
                        self.on_status("Enter the receiving laptop's LAN IP address first.")
                        continue
                    port = self.remote_config.get("lan_port", LAN_DEFAULT_PORT)
                    # Unlike a heartbeat, the receiver does real work here
                    # before responding - handle_new_clipboard_text()
                    # connects to (or launches) Word via COM, which can
                    # take several seconds (cold Word start, a
                    # OneDrive-synced document path, etc.). A short timeout
                    # here caused the sender to give up and close the
                    # connection while the receiver was still mid-connect,
                    # which surfaced as a ConnectionResetError on the
                    # receiver's end_headers() call even though typing had
                    # already been kicked off - confusing on both sides.
                    resp = requests.post(
                        f"http://{ip}:{port}/", data=payload, headers={"Content-Type": "text/plain"}, timeout=20
                    )
                    self.update_remote_paused_from_response(resp)
                else:
                    topic = self.remote_config["topic"]
                    resp = requests.post(
                        f"{NTFY_BASE_URL}/{topic}", data=payload, headers={"Content-Type": "text/plain"}, timeout=10
                    )
                resp.raise_for_status()
                # Only mark as sent on success - same reasoning as
                # clipboard_watch_loop: a transient network failure should
                # retry on the next poll rather than being silently dropped.
                self.last_sent_clipboard_text = current
                self.on_status("Sent to remote laptop.")
            except Exception as exc:
                self.on_status(f"Could not reach remote relay: {exc} - will retry.")

    def ensure_word_open(self, file_path):
        """Connects to (or launches) Word with the target document open,
        via COM rather than window-title matching - get_word_document()
        already handles "already open vs. needs opening/creating"
        transparently, and reliably reconnects to the SAME Word instance
        across threads/calls (see its docstring)."""
        doc = get_word_document(file_path)
        if doc is None:
            self.on_status("Could not open the Word document.")
            return False
        try:
            self.word_hwnd = doc.ActiveWindow.Hwnd
        except Exception:
            self.on_status("Could not find the Word window after opening it.")
            return False
        return True

    def prelaunch_word(self, file_path):
        # Runs on its own freshly-spawned thread (see toggle_watch /
        # toggle_remote_watch) which hasn't touched COM before - same
        # per-thread init requirement as every other Word-COM-touching
        # thread in this app.
        pythoncom.CoInitialize()
        try:
            if self.ensure_word_open(file_path):
                self.on_status("Watching clipboard - copy something to auto-type it into Word.")
        finally:
            pythoncom.CoUninitialize()

    # ---- shared status/control helpers ----

    def on_status(self, text):
        self.root.after(0, lambda: self.status_label.config(text=text))

    def set_remote_status(self, text):
        self.root.after(0, lambda: self.remote_status_label.config(text=f"Remote trigger: {text}"))

    def set_relay_status(self, text):
        self.root.after(0, lambda: self.relay_status_label.config(text=f"Relay: {text}"))

    def on_progress(self, done, total):
        self.root.after(0, lambda: self.status_label.config(text=f"Typing... {done}/{total} words"))

    def on_done_auto(self):
        self.root.after(0, self.reset_button_states)

    def set_controls_running(self):
        self.pause_btn.config(text="Pause", state="normal")
        self.stop_btn.config(state="normal")
        self.paused = False

    def reset_button_states(self):
        self.pause_btn.config(text="Pause", state="disabled")
        self.stop_btn.config(state="disabled")
        self.paused = False


def main():
    # Set only here, not at module import time - importing main.py from a
    # test script shouldn't silently redirect that test's own exceptions
    # into crash.log instead of showing up where the test can see them.
    def log_main_thread_exception(exc_type, exc_value, exc_tb):
        log_exception("main thread", exc_type, exc_value, exc_tb)

    def log_background_thread_exception(args):
        log_exception(f"thread '{args.thread.name}'", args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = log_main_thread_exception
    threading.excepthook = log_background_thread_exception

    root = tk.Tk()
    # Tkinter already intercepts exceptions raised inside widget callbacks
    # (button commands, etc.) - by default it just prints them to stderr,
    # invisible under pythonw.exe. Route those into the same log too.
    root.report_callback_exception = lambda exc_type, exc_value, exc_tb: log_exception(
        "tkinter callback", exc_type, exc_value, exc_tb
    )
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
