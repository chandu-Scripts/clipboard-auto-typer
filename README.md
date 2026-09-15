# Clipboard Auto Typer

A Windows desktop utility that watches your clipboard and automatically "types" whatever you copy into a chosen Microsoft Word document, at a configurable, natural-looking pace.

Copy text anywhere — an AI response, a document, dictated text — and it appears in Word progressively, as if it were being typed live, instead of pasted instantly.

## Features

- **Clipboard-triggered**: copy text anywhere on your system and it starts appearing in Word automatically — no manual paste needed.
- **Adjustable typing speed** (20–300 WPM) via a slider.
- **Customizable font**: set the font name and size typed text uses — a real, persistent Word document property, so it doesn't reset itself the way Notepad's zoom/display font used to.
- **Pause / Resume**:
  - Automatically pauses the moment you switch away from Word (e.g. to reply to a message), and resumes exactly where it left off once you switch back — so it never competes with you for keyboard input in another app.
  - A global hotkey (`Ctrl+Alt+P`) also lets you pause/resume manually from any app.
  - In laptop-to-laptop setups, the same hotkey works from **either** laptop: pressed on the Sends laptop, it sends a pause/resume command to the Types laptop's typing job instead of doing nothing locally — so you don't need to walk over to the other machine.
- **Reliable by construction**: text is written directly into the Word document via Word's COM automation (`Document.Content.InsertAfter`) rather than simulated keystrokes, so it can't drop or corrupt characters, and doesn't depend on which window happens to have focus at any given instant.
- Auto-launches Word with your chosen document if it isn't already open.
- **Laptop-to-laptop**: copy on one Windows laptop and it types into Word on a second one, over the internet or a shared local network, within a second or two, at the receiving laptop's speed slider — see [Laptop-to-Laptop](#laptop-to-laptop) below.
- **Mode switch**: each install of the app is either **Types** (writes to a local Word document) or **Sends** (relays its clipboard to another laptop in Types mode) — only the controls relevant to that role are shown, and a status line shows whether the relay connection is actually alive.
- **Remembers your setup**: mode, connection type, Word document path, speed, font, and whether a toggle was on are all restored automatically on the next launch — no need to re-select everything every time you restart the app.
- **Runs in the background**: closing the window minimizes it to the system tray instead of quitting; right-click the tray icon for **Show Window** or **Exit**.
- **Test Connection** button (Sends mode): checks connectivity on demand instead of waiting for the automatic status to update.
- **Crash logging**: since the app runs without a visible console, any unexpected error is written to `crash.log` in the project folder instead of silently vanishing.

## Requirements

- Windows 10/11
- Python 3.9+
- Microsoft Word, on any laptop running in **Types** mode (typing happens via Word's own COM automation). **Sends** mode has no such requirement.

## Installation

```bash
git clone https://github.com/<your-username>/clipboard-auto-typer.git
cd clipboard-auto-typer
pip install -r requirements.txt
```

## Usage

```bash
python main.py
```

1. Choose (or browse to) the Word document you want text typed into.
2. Set your preferred typing speed and font.
3. Click **Enable Clipboard Auto-Type**.
4. Copy any text — it will appear in Word automatically, at the pace you set.

While it's typing, switch to another app freely — generation pauses immediately and resumes exactly where it left off as soon as you switch back to Word.

### Controls

| Control | Effect |
|---|---|
| **Pause** / **Resume** button | Manually pause or resume typing |
| **Stop** button | Cancel the current job |
| `Ctrl+Alt+P` | Global pause/resume hotkey, works from any app |

## How it works

The app polls the clipboard for changes on a background thread. When new text is detected, it connects to (or launches) Word via [COM automation](https://learn.microsoft.com/en-us/office/vba/api/word.document) and appends the text word by word using `Document.Content.InsertAfter`, rather than simulating keystrokes — this is what makes typing reliable and focus-independent, while pause/resume is layered on top as a deliberate choice so the tool never interferes with input in whatever app you're actually using.

## Laptop-to-Laptop

Copy-paste normally only works on the same machine. This app can relay clipboard text between two Windows laptops two ways — pick whichever fits where the laptops actually are, via the **Connection** selector in the app:

- **Internet Relay** — works from anywhere, routed through [ntfy.sh](https://ntfy.sh), a free, no-signup pub/sub service. Depends on that third party and its free-tier daily message quota.
- **Local Network (LAN)** — only works when both laptops share a network (same WiFi/router), but then it's a direct connection with no internet hop and no quota at all.

### One-time setup

1. Generate a random topic name and secret key (anything long and hard to guess works — for example, run this once):
   ```bash
   python -c "import secrets; print('topic:', secrets.token_urlsafe(9)); print('secret:', secrets.token_urlsafe(18))"
   ```
2. Create `remote_config.json` in the project folder on **both** laptops (copy `remote_config.example.json` and fill in the same values on each):
   ```json
   {
     "topic": "your-random-topic",
     "secret": "your-random-secret",
     "lan_port": 8765,
     "lan_receiver_ip": ""
   }
   ```
   This file is git-ignored and never uploaded anywhere — keep it private, since anyone with the secret could type into your Word document. `topic` is only used by Internet Relay; `lan_port`/`lan_receiver_ip` only by LAN (the default port rarely needs changing, and `lan_receiver_ip` gets filled in automatically the first time you use LAN mode as the sender — see below).

### Using it (Internet Relay)

1. On the **receiving** laptop: select **This laptop: Types** and **Connection: Internet Relay**, set the Word document path, click **Enable Web Remote Trigger**.
2. On the **sending** laptop: select **This laptop: Sends** and **Connection: Internet Relay**, click **Send Clipboard to Remote Laptop**.
3. Both sides' status lines show **Connected** within a few seconds once both are running — this means each side has actually confirmed it's hearing from the other, not just that it can reach the relay.
4. Copy anything on the sending laptop — it appears in the receiving laptop's Word document within a second or two, at the receiving laptop's speed slider setting.

### Using it (Local Network / LAN)

1. On the **receiving** laptop: select **This laptop: Types** and **Connection: Local Network (LAN)**, set the Word document path, click **Enable Web Remote Trigger**. The status line shows the laptop's own LAN address, e.g. `Listening on 192.168.1.42:8765 - waiting for sending laptop...`.
2. On the **sending** laptop: select **This laptop: Sends** and **Connection: Local Network (LAN)**, enter the receiving laptop's IP (shown in step 1) into **Receiver's LAN IP**, click **Send Clipboard to Remote Laptop**. That IP is remembered in `remote_config.json` for next time.
3. Both sides show **Connected** once a message has actually gone through.
4. Copy anything on the sending laptop — it appears in the receiving laptop's Word document within a second or two, at the receiving laptop's speed slider setting.

Switching a laptop's mode or connection type automatically turns off whatever was previously running (e.g. selecting **Sends** stops **Enable Web Remote Trigger** if it was on; switching from Internet Relay to LAN stops an active internet-relay connection), since a laptop can only really play one role, on one transport, at a time.

**First-time LAN use**: the receiving laptop opens a small local server, so the first time you enable it, Windows Firewall may prompt to allow `python.exe` (or `pythonw.exe`) to accept incoming network connections — allow it, at least for private/home networks, or the sending laptop won't be able to reach it.

## License

[MIT](LICENSE)
