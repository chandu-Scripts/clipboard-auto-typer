import os
import re
import subprocess
import threading
import time
import tkinter as tk
from tkinter import filedialog

import keyboard
import pyperclip
import uiautomation as auto
import win32api
import win32con
import win32gui
import win32process

CLIPBOARD_POLL_SECONDS = 0.4
# Avoid letters (ctrl+<letter> can leak through as a real shortcut on a
# mistimed press - ctrl+alt+p briefly looking like ctrl+p = Print was exactly
# that) and avoid F10 (Windows treats it as a special system key that can
# activate a window's menu bar on its own, which is what opened a new
# untitled file via File > New). Insert has neither problem.
PAUSE_HOTKEY = "ctrl+alt+insert"
HOTKEY_DEBOUNCE_SECONDS = 0.5

# Typing is done via UI Automation (see get_notepad_value_pattern), not
# simulated keystrokes. Two independent problems with keyboard.write()/
# send() led to this:
#   1. It only ever reaches whichever window has OS focus at the moment
#      Windows actually delivers the event - not the moment this script
#      checked focus - so switching apps mid-word could leak/corrupt text
#      into the wrong window no matter how often focus was re-checked.
#   2. Measured directly: even with Notepad focused the ENTIRE time (no
#      app-switching involved at all), keyboard.write() intermittently drops
#      a character and duplicates the next one (e.g. "my age is" came out
#      as "yy gge is"). This is a known flakiness of the low-level
#      SendInput-based injection keyboard.write() uses, not something a
#      per-keystroke delay reliably fixes.
# A single UI Automation SetValue() call, by contrast, measured a fixed
# ~500ms cost regardless of text length and was correct in every test -
# because it sets the control's content directly/atomically rather than
# simulating individual key events. UIA_CALL_INTERVAL_SECONDS is that
# measured cost; AutoTyper batches enough words per call to keep pace with
# the requested WPM without calling more often than that can support.
UIA_CALL_INTERVAL_SECONDS = 0.5

TOKEN_PATTERN = re.compile(r"\S+|\s+")


def tokenize(text):
    """Splits text into (is_word, chunk) pairs, alternating between runs of
    non-whitespace and runs of whitespace (spaces, tabs, blank lines). Typing
    the chunks back out in order reproduces the original text exactly."""
    return [(not m.group()[0].isspace(), m.group()) for m in TOKEN_PATTERN.finditer(text)]


def get_notepad_value_pattern(hwnd, timeout=5.0):
    """Finds the UI Automation ValuePattern for a Notepad window's text
    editor, given its win32 window handle. This lets text be written
    directly into the control regardless of which window currently has OS
    focus - unlike keyboard.write()/send(), which always go to whatever
    window is in the foreground. Returns None if the window or its text
    control can no longer be found (e.g. Notepad was closed).

    Tries both the modern (Windows 11 Store) Notepad's DocumentControl and
    classic notepad.exe's EditControl, so this works against either. A
    freshly-launched Notepad's own UI Automation tree can take a moment to
    finish populating (separate from the win32 window itself already
    existing/being visible) - especially the first time in a session, since
    the modern Notepad is a WinUI3 app. `timeout` retries for that long
    instead of giving up after one quick check, which was causing "Could
    not find Notepad's text area" on a fresh launch even though Notepad had
    genuinely just opened."""
    deadline = time.time() + timeout
    while True:
        if not win32gui.IsWindow(hwnd):
            return None
        try:
            window_ctrl = auto.ControlFromHandle(hwnd)
            remaining = max(deadline - time.time(), 0.3)
            wait = min(remaining, 1.0)
            for finder in (window_ctrl.DocumentControl, window_ctrl.EditControl):
                text_ctrl = finder(searchDepth=10)
                if text_ctrl.Exists(wait):
                    value_pattern = text_ctrl.GetValuePattern()
                    if value_pattern is not None:
                        return value_pattern
        except Exception:
            pass
        if time.time() >= deadline:
            return None
        time.sleep(0.2)


class AutoTyper:
    """Writes tokenized text into a specific target window via UI
    Automation, in word batches paced to approximate a controllable WPM
    speed. Runs on its own thread; pause/resume/stop are signaled via
    threading events so the caller stays responsive. Pauses automatically
    whenever the target window doesn't have OS focus (see
    _wait_until_ready) and resumes exactly where it left off once it does -
    this is a deliberate UX choice, not a technical requirement: because
    writes happen as atomic UI Automation batch calls rather than
    individual keystrokes, this pause is corruption-free by construction,
    unlike the old keystroke-based approach. Pausing while unfocused means
    the user can freely type in another app without any background writes
    landing there or competing for input."""

    def __init__(self):
        self.running_event = threading.Event()  # set = not paused
        self.running_event.set()
        self.stop_flag = threading.Event()
        self.thread = None

    def start(self, tokens, wpm, on_status, on_progress, on_done, target_hwnd):
        self.stop_flag.clear()
        self.running_event.set()
        self.thread = threading.Thread(
            target=self._run,
            args=(tokens, wpm, on_status, on_progress, on_done, target_hwnd),
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

    def _run(self, tokens, wpm, on_status, on_progress, on_done, target_hwnd):
        # UI Automation must be explicitly initialized on every thread that
        # uses it, per the uiautomation library's own docs - without this,
        # UIA/Control/Pattern calls made from a spawned thread (this one)
        # rather than the main thread fail silently instead of raising a
        # catchable exception, which looked like the whole job just hanging
        # or doing nothing at all.
        auto.InitializeUIAutomationInCurrentThread()
        seconds_per_word = 60.0 / wpm
        # Enough words per UIA call to roughly keep pace with the requested
        # WPM without calling more often than UIA_CALL_INTERVAL_SECONDS
        # supports (each call costs about that much regardless of length).
        words_per_batch = max(1, round(UIA_CALL_INTERVAL_SECONDS / seconds_per_word))
        total_words = sum(1 for is_word, _ in tokens if is_word)

        value_pattern = get_notepad_value_pattern(target_hwnd)
        if value_pattern is None:
            on_status("Could not find Notepad's text area.")
            return

        written_parts = []
        done_words = 0
        words_since_flush = 0

        def flush():
            if not win32gui.IsWindow(target_hwnd):
                on_status("Notepad window closed - stopped.")
                return False
            try:
                value_pattern.SetValue("".join(written_parts))
            except Exception as exc:
                on_status(f"Could not write to Notepad: {exc}")
                return False
            on_progress(done_words, total_words)
            return True

        for is_word, chunk in tokens:
            if not self._wait_until_ready(target_hwnd, on_status):
                return  # stopped, or window closed - status already set

            written_parts.append(chunk)
            if is_word:
                done_words += 1
                words_since_flush += 1

            if words_since_flush >= words_per_batch:
                call_start = time.time()
                if not flush():
                    return
                words_since_flush = 0
                target_duration = seconds_per_word * words_per_batch
                time.sleep(max(0, target_duration - (time.time() - call_start)))

        if not flush():  # final flush for any remaining tail (last partial batch, trailing whitespace)
            return
        on_status("Done")
        on_done()

    def _wait_until_ready(self, target_hwnd, on_status):
        """Blocks until not manually paused AND Notepad has OS focus, so
        switching away pauses generation immediately (nothing more gets
        written) and switching back resumes it exactly where it left off -
        this is what lets the user freely type in another app (WhatsApp,
        Teams, ...) without the background writes interfering there.
        Checked once per token (word or whitespace run), not per-character -
        since writing now happens as atomic UI Automation batch calls
        rather than individual keystrokes, there's no risk of a corrupted
        partial word from pausing here, so this doesn't need to be any
        finer-grained than that. Returns False (with on_status already set)
        if stop() was called or the target window has been closed."""
        announced = False
        while True:
            self.running_event.wait()
            if self.stop_flag.is_set():
                on_status("Stopped")
                return False
            if not win32gui.IsWindow(target_hwnd):
                on_status("Notepad window closed - stopped.")
                return False
            try:
                has_focus = win32gui.GetForegroundWindow() == target_hwnd
            except Exception:
                has_focus = True  # can't tell - don't get stuck waiting forever
            if has_focus:
                return True
            if not announced:
                on_status("Paused - switch back to Notepad to resume typing.")
                announced = True
            time.sleep(0.15)


def find_window_by_title_substring(substring, timeout=5.0):
    """Finds a visible top-level window whose title contains the given text.
    Matching by PID doesn't work reliably here: on Windows 11, notepad.exe is
    often the Store-packaged app, launched through an execution alias, so the
    process this script starts isn't necessarily the one that owns the
    window. Title matching sidesteps that entirely."""
    deadline = time.time() + timeout
    substring_lower = substring.lower()
    while time.time() < deadline:
        matches = []

        def callback(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if title and substring_lower in title.lower():
                    matches.append(hwnd)
            return True

        win32gui.EnumWindows(callback, None)
        if matches:
            return matches[0]
        time.sleep(0.15)
    return None


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


class App:
    def __init__(self, root):
        self.root = root
        root.title("Clipboard Auto Typer")
        root.geometry("560x230")

        path_frame = tk.Frame(root)
        path_frame.pack(fill="x", padx=10, pady=(12, 6))
        tk.Label(path_frame, text="Notepad file:").pack(side="left")
        self.file_path_var = tk.StringVar()
        self.file_path_entry = tk.Entry(path_frame, textvariable=self.file_path_var)
        self.file_path_entry.pack(side="left", fill="x", expand=True, padx=(6, 6))
        self.browse_btn = tk.Button(path_frame, text="Browse...", command=self.browse_file)
        self.browse_btn.pack(side="left")

        control_frame = tk.Frame(root)
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

        watch_frame = tk.Frame(root)
        watch_frame.pack(fill="x", padx=10, pady=(0, 6))
        self.watch_btn = tk.Button(watch_frame, text="Enable Clipboard Auto-Type", command=self.toggle_watch)
        self.watch_btn.pack(side="left")

        tk.Label(root, text=f"Pause/Resume hotkey: {PAUSE_HOTKEY} (works from any app)", fg="gray").pack(
            anchor="w", padx=10, pady=(0, 4)
        )
        self.status_label = tk.Label(root, text="Idle", anchor="w")
        self.status_label.pack(fill="x", padx=10, pady=(0, 10))

        self.typer = AutoTyper()
        self.paused = False

        self.clipboard_watch_enabled = False
        self.last_clipboard_text = ""
        self.notepad_hwnd = None

        self.last_hotkey_time = 0.0

        threading.Thread(target=self.clipboard_watch_loop, daemon=True).start()
        keyboard.add_hotkey(PAUSE_HOTKEY, self.on_pause_hotkey, suppress=True)

    # ---- pause/stop controls ----

    def on_pause_hotkey(self):
        now = time.time()
        if now - self.last_hotkey_time < HOTKEY_DEBOUNCE_SECONDS:
            return  # ignore OS key-repeat firing this multiple times per press
        self.last_hotkey_time = now
        self.root.after(0, self.handle_pause_hotkey)

    def handle_pause_hotkey(self):
        if self.typer.is_running():
            self.on_pause()

    def on_pause(self):
        if not self.paused:
            self.typer.pause()
            self.paused = True
            self.pause_btn.config(text="Resume")
            self.status_label.config(text="Paused")
        else:
            self.typer.resume()
            self.paused = False
            self.pause_btn.config(text="Pause")

    def on_stop(self):
        self.typer.stop()
        self.reset_button_states()

    # ---- clipboard auto-type flow ----

    def browse_file(self):
        path = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if path:
            self.file_path_var.set(path)

    def toggle_watch(self):
        if not self.clipboard_watch_enabled:
            file_path = self.file_path_var.get().strip()
            if not file_path:
                self.status_label.config(text="Select a Notepad file path first.")
                return
            self.clipboard_watch_enabled = True
            self.last_clipboard_text = self.safe_paste()
            self.watch_btn.config(text="Disable Clipboard Auto-Type")
            self.file_path_entry.config(state="disabled")
            self.browse_btn.config(state="disabled")
            self.status_label.config(text="Opening Notepad...")
            # Pre-launch/find Notepad now instead of waiting for the first
            # copy - that first launch is the slow part (spawning the
            # process and waiting for its window), so do it up front.
            threading.Thread(target=self.prelaunch_notepad, args=(file_path,), daemon=True).start()
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
        # this thread also makes UI Automation calls directly (clearing
        # Notepad before a new job starts), so it needs its own init too.
        auto.InitializeUIAutomationInCurrentThread()
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
                # before) meant a transient failure - e.g. Notepad's UI
                # Automation tree not being ready yet right after a fresh
                # launch - would silently and PERMANENTLY blacklist that
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
        """Attempts to start typing `text` into Notepad. Returns True only if
        a typing job was actually started - the caller uses this to decide
        whether this clipboard content may be safely considered "handled",
        so a transient failure gets retried on the next poll instead of
        being silently and permanently ignored."""
        tokens = tokenize(text)
        if not any(is_word for is_word, _ in tokens):
            return True  # nothing to type, but not a failure - don't retry it

        file_path = self.file_path_var.get().strip()
        if not file_path:
            self.on_status("Set a Notepad file path first, then copy again.")
            return False

        self.typer.stop_and_wait()

        if not self.ensure_notepad_open(file_path):
            return False

        # Clear via UI Automation rather than Ctrl+A/Delete keystrokes - it
        # doesn't depend on Notepad actually having focus at this exact
        # moment to land correctly. Bringing Notepad to the foreground is
        # now just a nicety (so the user sees typing start if they're
        # looking), not something the clear itself depends on.
        value_pattern = get_notepad_value_pattern(self.notepad_hwnd)
        if value_pattern is None:
            self.on_status("Could not find Notepad's text area - will retry.")
            return False
        try:
            value_pattern.SetValue("")
        except Exception as exc:
            self.on_status(f"Could not clear Notepad: {exc} - will retry.")
            return False
        try:
            force_foreground(self.notepad_hwnd)
        except Exception:
            pass  # best-effort only - typing doesn't depend on this succeeding

        self.root.after(0, self.set_controls_running)
        self.typer.start(
            tokens,
            self.wpm_var.get(),
            self.on_status,
            self.on_progress,
            self.on_done_auto,
            target_hwnd=self.notepad_hwnd,
        )
        return True

    def ensure_notepad_open(self, file_path):
        filename = os.path.basename(file_path)

        # Always re-check for an already-open window matching this exact file
        # first, rather than trusting a cached handle - launching notepad.exe
        # again when it's already open can open a second window/tab instead
        # of reusing the visible one.
        hwnd = find_window_by_title_substring(filename, timeout=0.3)
        if hwnd is not None:
            self.notepad_hwnd = hwnd
            return True

        try:
            if not os.path.exists(file_path):
                open(file_path, "w").close()
            subprocess.Popen(["notepad.exe", file_path])
        except OSError as exc:
            self.on_status(f"Could not open Notepad file: {exc}")
            return False
        hwnd = find_window_by_title_substring(filename)
        if hwnd is None:
            self.on_status("Could not find the Notepad window after opening it.")
            return False
        self.notepad_hwnd = hwnd
        return True

    def prelaunch_notepad(self, file_path):
        if self.ensure_notepad_open(file_path):
            self.on_status("Watching clipboard - copy something to auto-type it into Notepad.")

    # ---- shared status/control helpers ----

    def on_status(self, text):
        self.root.after(0, lambda: self.status_label.config(text=text))

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
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
