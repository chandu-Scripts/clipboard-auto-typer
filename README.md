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

## License

[MIT](LICENSE)
