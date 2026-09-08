# Clipboard Auto Typer

A Windows desktop utility that watches your clipboard and automatically "types" whatever you copy into a chosen Notepad file, at a configurable, natural-looking pace.

Copy text anywhere — an AI response, a document, dictated text — and it appears in Notepad progressively, as if it were being typed live, instead of pasted instantly.

## Features

- **Clipboard-triggered**: copy text anywhere on your system and it starts appearing in Notepad automatically — no manual paste needed.
- **Adjustable typing speed** (20–300 WPM) via a slider.
- **Pause / Resume**:
  - Automatically pauses the moment you switch away from Notepad (e.g. to reply to a message), and resumes exactly where it left off once you switch back — so it never competes with you for keyboard input in another app.
  - A global hotkey (`Ctrl+Alt+Insert`) also lets you pause/resume manually from any app.
- **Reliable by construction**: text is written directly into Notepad's text control via Windows UI Automation rather than simulated keystrokes, so it can't drop or corrupt characters, and doesn't depend on which window happens to have focus at any given instant.
- **Works with both** the modern Windows 11 (Store) Notepad and the classic `notepad.exe`.
- Auto-launches Notepad with your chosen file if it isn't already open.
- **Laptop-to-laptop**: copy on one Windows laptop and it types into Notepad on a second one, over the internet, within a second or two, at the receiving laptop's speed slider — see [Laptop-to-Laptop](#laptop-to-laptop) below.
- **Mode switch**: each install of the app is either **Types** (writes to a local Notepad file) or **Sends** (relays its clipboard to another laptop in Types mode) — only the controls relevant to that role are shown, and a status line shows whether the relay connection is actually alive.

## Requirements

- Windows 10/11
- Python 3.9+

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

1. Choose (or browse to) the Notepad file you want text typed into.
2. Set your preferred typing speed.
3. Click **Enable Clipboard Auto-Type**.
4. Copy any text — it will appear in Notepad automatically, at the pace you set.

While it's typing, switch to another app freely — generation pauses immediately and resumes exactly where it left off as soon as you switch back to Notepad.

### Controls

| Control | Effect |
|---|---|
| **Pause** / **Resume** button | Manually pause or resume typing |
| **Stop** button | Cancel the current job |
| `Ctrl+Alt+Insert` | Global pause/resume hotkey, works from any app |

## How it works

The app polls the clipboard for changes on a background thread. When new text is detected, it locates (or launches) the target Notepad window and writes the text into it word by word via [UI Automation](https://learn.microsoft.com/en-us/windows/win32/winauto/entry-uiautomation-win32), rather than simulating keystrokes — this is what makes typing reliable and focus-independent, while pause/resume is layered on top as a deliberate choice so the tool never interferes with input in whatever app you're actually using.

## Laptop-to-Laptop

Copy-paste normally only works on the same machine. This app can also relay clipboard text between two Windows laptops over the internet, through [ntfy.sh](https://ntfy.sh) — a free, no-signup pub/sub service — so copying on one laptop types into Notepad on the other.

### One-time setup

1. Generate a random topic name and secret key (anything long and hard to guess works — for example, run this once):
   ```bash
   python -c "import secrets; print('topic:', secrets.token_urlsafe(9)); print('secret:', secrets.token_urlsafe(18))"
   ```
2. Create `remote_config.json` in the project folder on **both** laptops (copy `remote_config.example.json` and fill in the same values on each):
   ```json
   {
     "topic": "your-random-topic",
     "secret": "your-random-secret"
   }
   ```
   This file is git-ignored and never uploaded anywhere — keep it private, since anyone with both the topic and secret could type into your Notepad.

### Using it

1. On the **receiving** laptop: select **This laptop: Types**, set the Notepad file path, click **Enable Web Remote Trigger**. The "Remote trigger:" line shows **Connected** once the relay link is live.
2. On the **sending** laptop: select **This laptop: Sends**, click **Send Clipboard to Remote Laptop**. The "Relay:" line shows **Reachable** once it can reach the network.
3. Copy anything on the sending laptop — it appears on the receiving laptop's Notepad within a second or two, at the receiving laptop's speed slider setting.

Switching a laptop's mode automatically turns off whatever the other mode's controls were doing (e.g. selecting **Sends** stops **Enable Web Remote Trigger** if it was on), since that laptop can only really play one role at a time with a given `remote_config.json`.

## License

[MIT](LICENSE)
