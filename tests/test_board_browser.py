"""Real Chromium UI/audio tests, with fake microphone audio and mocked providers.

No real API key, paid service, clipboard monitor, Word, or microphone is used.
"""
import json
import time
import uuid
from pathlib import Path

import pytest
import requests
from playwright.sync_api import sync_playwright, expect

import speech_config
from test_speech import board_server, credentials  # shared isolated fixtures
from test_remote_mic import lan_mic_receiver


FAKE_RECOGNITION = """
window.recognizers = [];
window.speechEvents = [];
class FakeRecognition {
  constructor() { window.recognizers.push(this); this.aborted = false; }
  start() { setTimeout(() => this.onstart && this.onstart(), 10); }
  stop() {
    speechEvents.push('free-stop');
    setTimeout(() => {
      if (this.tail) this.emit([{text:this.tail,final:true}]);
      this.onend && this.onend();
    }, 50);
  }
  abort() { this.aborted = true; this.onend && this.onend(); }
  emit(items, index=0) {
    this.onresult({resultIndex:index, results:items.map(item => {
      const r=[{transcript:item.text}]; r.isFinal=item.final; return r;
    })});
  }
}
window.SpeechRecognition = FakeRecognition;
"""


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel="chrome", headless=True,
            args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"],
        )
        yield browser
        browser.close()


@pytest.fixture
def page(browser, board_server):
    _, url = board_server
    context = browser.new_context(viewport={"width": 1366, "height": 900})
    context.add_init_script(FAKE_RECOGNITION)
    context.grant_permissions(["microphone", "clipboard-read", "clipboard-write"])
    page = context.new_page()
    page.errors = []
    page.on("pageerror", lambda error: page.errors.append(str(error)))
    page.goto(url)
    expect(page.locator("#link-text")).to_have_text("Connected")
    yield page
    context.close()
    assert not page.errors


def test_free_preview_graceful_stop_and_no_tab_restart(page):
    page.locator("#mic").click()
    expect(page.locator("#mic-note")).to_have_text("Listening · Free")
    page.evaluate("recognizers[0].emit([{text:'first ',final:false},{text:'second',final:false}])")
    page.evaluate("recognizers[0].emit([{text:'first ',final:false},{text:'revised',final:false}],1)")
    expect(page.locator("#interim")).to_have_text("first revised")
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    assert page.evaluate("recognizers.length") == 1
    assert not page.evaluate("recognizers[0].aborted")
    page.evaluate("recognizers[0].tail='first revised'")
    page.locator("#mic").click()
    expect(page.locator("#committed")).to_have_text("first revised")
    expect(page.locator("#mic-note")).to_have_text("Microphones off")
    assert not page.evaluate("recognizers[0].aborted")


def test_pending_delivery_retries_in_order(page):
    calls = []

    def flaky(route):
        calls.append(route.request.post_data_json)
        if len(calls) == 1:
            route.abort()
        else:
            route.continue_()

    page.route("**/append", flaky)
    page.locator("#mic").click()
    expect(page.locator("#mic-note")).to_have_text("Listening · Free")
    page.evaluate("recognizers[0].emit([{text:'first',final:true},{text:'second',final:true}])")
    expect(page.locator("#committed")).to_have_text("first second")
    assert [call["text"] for call in calls] == ["first", "first", "second"]
    assert calls[0]["id"] == calls[1]["id"]
    expect(page.locator("#speech-error")).to_have_text("")
    page.locator("#mic").click()


def test_key_controls_and_reload(page, credentials, monkeypatch):
    monkeypatch.setattr(speech_config, "create_token", lambda key=None: {"token":"test-token", "url":speech_config.REALTIME_URL})
    page.locator("#speech-settings").click()
    page.locator("#speech-key").fill("dummy-browser-key")
    page.locator("#speech-key-save").click()
    expect(page.locator("#speech-key-status")).to_have_text("Key saved for this Windows user.")
    expect(page.locator("#speech-key")).to_have_value("")
    assert "dummy-browser-key" not in credentials.read_text()
    page.reload()
    page.locator("#speech-settings").click()
    expect(page.locator("#speech-key-status")).to_have_text("Key saved for this Windows user.")
    page.locator("#speech-key-test").click()
    expect(page.locator("#speech-key-status")).to_contain_text("Key accepted")
    page.locator("#speech-key-remove").click()
    expect(page.locator("#speech-key-status")).to_have_text("No Speechmatics key saved on this laptop.")
    assert not credentials.exists()


@pytest.mark.parametrize("use_shortcuts", [False, True, "layout", "windows_layout"])
def test_switch_to_paid_streams_pcm_and_finishes_before_free(page, monkeypatch, use_shortcuts):
    monkeypatch.setattr(speech_config, "create_token", lambda: {"token":"test-token", "url":speech_config.REALTIME_URL})
    messages, audio = [], []

    def websocket(ws):
        def receive(data):
            if isinstance(data, bytes):
                audio.append(data)
                return
            msg = json.loads(data)
            messages.append(msg)
            if msg["message"] == "StartRecognition":
                ws.send(json.dumps({"message":"RecognitionStarted", "id":"test-session"}))
                ws.send(json.dumps({"message":"AddPartialTranscript", "metadata":{"transcript":"paid preview"}}))
            elif msg["message"] == "EndOfStream":
                ws.send(json.dumps({"message":"AddTranscript", "metadata":{"transcript":"paid final", "start_time":0,"end_time":1}}))
                ws.send(json.dumps({"message":"EndOfTranscript"}))
        ws.on_message(receive)

    page.route_web_socket("wss://eu.rt.speechmatics.com/**", websocket)
    def toggle(kind):
        if use_shortcuts == "windows_layout":
            # Flags captured from real Windows Ctrl+left Alt+O/M input:
            # the letter reports only AltGraph, without ctrlKey or altKey.
            page.evaluate("""kind => {
                const send = (type, options) => document.dispatchEvent(new KeyboardEvent(type,
                    {bubbles:true, cancelable:true, ...options}));
                send('keydown', {key:'Control', code:'ControlLeft', ctrlKey:true});
                send('keydown', {key:'Alt', code:'AltLeft', ctrlKey:true, altKey:true});
                const letter = {code:kind === 'free' ? 'KeyO' : 'KeyM',
                    key:kind === 'free' ? '\u014d' : '\u1e41', modifierAltGraph:true};
                send('keydown', letter);
                send('keyup', letter);
                send('keyup', {key:'Alt', code:'AltLeft', ctrlKey:true});
                send('keyup', {key:'Control', code:'ControlLeft'});
            }""", kind)
        elif use_shortcuts == "layout":
            # Ctrl+Alt can change event.key on international keyboard layouts.
            page.evaluate("""kind => document.dispatchEvent(new KeyboardEvent('keydown', {
                code: kind === 'free' ? 'KeyO' : 'KeyM',
                key: kind === 'free' ? '\u00f8' : '\u00b5',
                ctrlKey: true, altKey: true, bubbles: true, cancelable: true
            }))""", kind)
        elif use_shortcuts:
            page.keyboard.press("Control+Alt+" + ("o" if kind == "free" else "m"))
        else:
            page.locator("#mic" if kind == "free" else "#mic-paid").click()

    toggle("free")
    expect(page.locator("#mic-note")).to_have_text("Listening · Free")
    page.evaluate("recognizers[0].tail='free final'")
    toggle("paid")
    expect(page.locator("#mic-note")).to_have_text("Listening · Speechmatics (paid)")
    expect(page.locator("#committed")).to_have_text("free final")
    expect(page.locator("#interim")).to_have_text("paid preview")
    assert messages[0]["audio_format"]["encoding"] == "pcm_s16le"
    assert messages[0]["transcription_config"]["model"] == "enhanced"
    # Actual AudioWorklet / fake-device capture, not a mocked PCM encoder.
    page.wait_for_timeout(250)
    toggle("free")
    expect(page.locator("#mic-note")).to_have_text("Listening · Free")
    expect(page.locator("#committed")).to_have_text("free final paid final")
    assert audio
    assert messages[-1] == {"message":"EndOfStream", "last_seq_no":len(audio)}
    assert all(len(chunk) % 2 == 0 for chunk in audio)
    assert page.evaluate("recognizers.length") == 2
    toggle("free")
    expect(page.locator("#mic-note")).to_have_text("Microphones off")
    toggle("paid")
    expect(page.locator("#mic-paid")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#mic-paid")).to_be_enabled()
    toggle("paid")
    expect(page.locator("#mic-note")).to_have_text("Microphones off")


def test_mic_shortcut_while_editing_toggles_once_per_press(page, board_server):
    server, _ = board_server
    text = "Keep the selected answer unchanged."
    server.hub.append(text)
    editor = page.locator("#committed")
    expect(editor).to_have_text(text)
    editor.focus()
    page.keyboard.press("Control+a")
    page.keyboard.down("Control")
    page.keyboard.down("Alt")
    page.keyboard.down("o")
    expect(page.locator("#mic-note")).to_have_text("Listening · Free")
    page.keyboard.down("o")  # Native repeated keydown while the key is held.
    page.keyboard.up("o")
    page.keyboard.up("Alt")
    page.keyboard.up("Control")
    expect(page.locator("#mic-note")).to_have_text("Listening · Free")
    expect(editor).to_have_text(text)
    assert page.evaluate("window.getSelection().toString()") == text
    assert page.evaluate("recognizers.length") == 1
    page.keyboard.press("Control+Alt+o")
    expect(page.locator("#mic-note")).to_have_text("Microphones off")
    expect(editor).to_have_text(text)


def test_altgr_typing_and_lost_focus_do_not_toggle_mics(page):
    assert page.evaluate("""() => {
        const send = (type, options) => document.dispatchEvent(new KeyboardEvent(type,
            {bubbles:true, cancelable:true, ...options}));
        send('keydown', {key:'Control', code:'ControlLeft', ctrlKey:true});
        send('keydown', {key:'AltGraph', code:'AltRight', modifierAltGraph:true});
        const allowed = send('keydown', {key:'\u1e41', code:'KeyM', modifierAltGraph:true});
        send('keyup', {key:'AltGraph', code:'AltRight'});
        send('keyup', {key:'Control', code:'ControlLeft'});
        return allowed;
    }""")
    # A modifier released outside the page must not leave a stuck shortcut.
    assert page.evaluate("""() => {
        const send = (options) => document.dispatchEvent(new KeyboardEvent('keydown',
            {bubbles:true, cancelable:true, ...options}));
        send({key:'Control', code:'ControlLeft', ctrlKey:true});
        send({key:'Alt', code:'AltLeft', ctrlKey:true, altKey:true});
        window.dispatchEvent(new Event('blur'));
        return send({key:'o', code:'KeyO'});
    }""")
    expect(page.locator('#mic-note')).to_have_text('Microphones off')
    assert page.evaluate('recognizers.length') == 0


def test_missing_paid_key_returns_to_off(page, credentials):
    page.locator("#mic-paid").click()
    expect(page.locator("#speech-error")).to_contain_text("Add your Speechmatics API key")
    expect(page.locator("#mic-note")).to_have_text("Microphones off")
    expect(page.locator("#mic")).to_be_enabled()
    expect(page.locator("#mic-paid")).not_to_have_class("live")


def test_network_error_does_not_show_listening(page):
    page.locator("#mic").click()
    expect(page.locator("#mic-note")).to_have_text("Listening · Free")
    page.evaluate("recognizers[0].onerror({error:'network'})")
    expect(page.locator("#mic-note")).to_contain_text("disconnected")
    assert "live" not in (page.locator("#mic").get_attribute("class") or "").split()
    page.locator("#mic").click()


def test_unfinished_preview_joins_board_after_browser_restart(page):
    page.locator("#mic").click()
    expect(page.locator("#mic-note")).to_have_text("Listening · Free")
    page.evaluate("recognizers[0].emit([{text:'unfinished technical phrase',final:false}])")
    page.evaluate("recognizers[0].onend()")
    expect(page.locator("#committed")).to_have_text("unfinished technical phrase")
    expect(page.locator("#speech-error")).to_have_text("")
    expect(page.locator("#speech-copy-pending")).to_have_count(0)
    expect(page.locator("#mic-note")).to_have_text("Listening · Free")
    assert page.evaluate("recognizers.length") == 2
    page.evaluate("recognizers[1].emit([{text:'new sentence',final:true}])")
    expect(page.locator("#committed")).to_have_text("unfinished technical phrase new sentence")
    page.locator("#mic").click()


def test_queued_final_survives_page_refresh(page):
    page.route("**/append", lambda route: route.abort())
    page.locator("#mic").click()
    expect(page.locator("#mic-note")).to_have_text("Listening · Free")
    page.evaluate("recognizers[0].emit([{text:'recover this transcript',final:true}])")
    expect(page.locator("#speech-error")).to_contain_text("Reconnecting to the board")
    page.locator("#mic").click()
    page.unroute("**/append")
    page.reload()
    expect(page.locator("#committed")).to_have_text("recover this transcript")
    expect(page.locator("#speech-error")).to_have_text("")


def test_paid_error_preserves_preview_and_stops(page, monkeypatch):
    monkeypatch.setattr(speech_config, "create_token", lambda: {"token":"test-token", "url":speech_config.REALTIME_URL})
    sockets = []

    def websocket(ws):
        sockets.append(ws)
        def receive(data):
            if isinstance(data, str) and json.loads(data)["message"] == "StartRecognition":
                ws.send(json.dumps({"message":"RecognitionStarted", "id":"test-session"}))
                ws.send(json.dumps({"message":"AddPartialTranscript", "metadata":{"transcript":"unfinished paid phrase"}}))
        ws.on_message(receive)

    page.route_web_socket("wss://eu.rt.speechmatics.com/**", websocket)
    page.locator("#mic-paid").click()
    expect(page.locator("#mic-note")).to_have_text("Listening · Speechmatics (paid)")
    expect(page.locator("#interim")).to_have_text("unfinished paid phrase")
    sockets[0].send(json.dumps({"message":"Error", "type":"insufficient_funds"}))
    expect(page.locator("#speech-error")).to_contain_text("needs more credit")
    expect(page.locator("#mic-note")).to_have_text("Microphones off")
    expect(page.locator("#committed")).to_have_text("unfinished paid phrase")
    expect(page.locator("#speech-copy-pending")).to_have_count(0)


def test_old_saved_previews_are_automatically_added_once(page):
    page.evaluate("sessionStorage.setItem('answerboard.speech-drafts.v1', JSON.stringify(['previous words']))")
    page.reload()
    expect(page.locator("#committed")).to_have_text("previous words")
    assert page.evaluate("sessionStorage.getItem('answerboard.speech-drafts.v1')") is None
    page.reload()
    expect(page.locator("#committed")).to_have_text("previous words")


def test_stop_without_final_result_keeps_words_on_board_once(page):
    page.locator("#mic").click()
    expect(page.locator("#mic-note")).to_have_text("Listening · Free")
    page.evaluate("recognizers[0].emit([{text:'last recognized words',final:false}])")
    page.locator("#mic").click()
    expect(page.locator("#committed")).to_have_text("last recognized words")
    page.locator("#mic").click()
    expect(page.locator("#mic-note")).to_have_text("Listening · Free")
    page.locator("#mic").click()
    expect(page.locator("#mic-note")).to_have_text("Microphones off")
    expect(page.locator("#committed")).to_have_text("last recognized words")


@pytest.mark.parametrize("edit_key", ["Backspace", "Delete", "x"])
def test_clicking_blank_space_preserves_selected_text(page, board_server, edit_key):
    server, _ = board_server
    paragraph = "First selected line.\nSecond selected line."
    text = paragraph + "\nKeep this last line."
    server.hub.append(text)
    editor = page.locator("#committed")
    expect(editor).to_have_text(text)

    def select_paragraph():
        editor.evaluate("""(el, length) => {
            el.focus();
            const range = document.createRange();
            range.setStart(el.firstChild, 0);
            range.setEnd(el.firstChild, length);
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
        }""", len(paragraph))
        assert page.evaluate("window.getSelection().toString()") == paragraph

    select_paragraph()
    board = page.locator("#text").bounding_box()
    page.mouse.click(board["x"] + board["width"] / 2, board["y"] + board["height"] - 40)
    expect(editor).to_have_text(text)
    assert page.evaluate("window.getSelection().isCollapsed")
    page.keyboard.type(" More text.")
    expect(editor).to_have_text(text + " More text.")

    select_paragraph()
    page.keyboard.press(edit_key)
    replacement = "x" if edit_key == "x" else ""
    expect(editor).to_have_text(replacement + "\nKeep this last line. More text.")


@pytest.mark.parametrize("width,height,enlarge", [(1366, 900, False), (768, 600, False), (1366, 900, True)])
def test_last_paragraph_can_scroll_to_top(page, board_server, width, height, enlarge):
    page.set_viewport_size({"width": width, "height": height})
    if enlarge:
        for _ in range(8):
            page.locator("#bigger").click()
    server, _ = board_server
    earlier = "Earlier paragraph with some transcript text.\n\n" * 24
    last = "Final paragraph. " + "More words on the final paragraph. " * 8
    server.hub.append(earlier + last)
    expect(page.locator("#committed")).to_have_text(earlier + last)
    page.wait_for_timeout(200)

    def paragraph_offset():
        return page.locator("#committed").evaluate("""(el, offset) => {
            const range = document.createRange();
            range.setStart(el.firstChild, offset);
            range.setEnd(el.firstChild, offset + 1);
            return range.getBoundingClientRect().top - document.getElementById('text').getBoundingClientRect().top;
        }""", len(earlier))

    board = page.locator("#text")
    bounds = board.bounding_box()
    assert bounds["y"] + bounds["height"] <= height + 1
    board.hover()
    page.mouse.wheel(0, paragraph_offset() - 32)
    page.wait_for_timeout(300)
    assert abs(paragraph_offset() - 32) <= 3
    expect(page.locator("#jump")).to_be_visible()
    reading_position = board.evaluate("el => el.scrollTop")
    server.hub.append("\nA newer paragraph has arrived.")
    expect(page.locator("#committed")).to_contain_text("A newer paragraph has arrived.")
    page.wait_for_timeout(100)
    assert abs(board.evaluate("el => el.scrollTop") - reading_position) <= 1

    page.locator("#jump").click()
    expect(page.locator("#jump")).to_be_hidden()
    assert board.evaluate("""el => Math.abs(el.scrollTop - Math.max(0,
        el.scrollHeight - el.clientHeight - parseFloat(getComputedStyle(el).paddingBottom) + 80))""") <= 4
    # At the end of the full scroll range, the final line remains visible near
    # the top rather than leaving the reader looking at a blank viewport.
    board.hover()
    page.mouse.wheel(0, 100000)
    page.wait_for_timeout(300)
    line_top = page.locator("#committed").evaluate("""el => {
        const node = el.lastChild;
        const range = document.createRange();
        range.setStart(node, node.length - 1);
        range.setEnd(node, node.length);
        return range.getBoundingClientRect().top - document.getElementById('text').getBoundingClientRect().top;
    }""")
    assert 20 <= line_top <= 55, board.evaluate("""el => ({height:el.clientHeight,
        scroll:el.scrollTop, total:el.scrollHeight, padding:getComputedStyle(el).paddingBottom,
        line:getComputedStyle(el).lineHeight, reader:el.style.getPropertyValue('--reader-height')})""")


def test_sender_controls_only_enabled_receiver_tab(page, lan_mic_receiver, monkeypatch):
    _, lan_url = lan_mic_receiver
    monkeypatch.setattr(speech_config, "create_token", lambda: {"token":"test-token", "url":speech_config.REALTIME_URL})
    def websocket(ws):
        def receive(data):
            if not isinstance(data, str):
                return
            kind = json.loads(data)["message"]
            if kind == "StartRecognition":
                ws.send(json.dumps({"message":"RecognitionStarted", "id":"remote-test"}))
            elif kind == "EndOfStream":
                ws.send(json.dumps({"message":"EndOfTranscript"}))
        ws.on_message(receive)
    page.route_web_socket("wss://eu.rt.speechmatics.com/**", websocket)
    page.locator("#speech-settings").click()
    page.locator("#remote-mic-enable").click()
    expect(page.locator("#remote-mic-enable")).to_have_attribute("aria-pressed", "true")
    page.locator("#speech-settings-close").click()

    other = page.context.new_page()
    other.goto(page.url)
    expect(other.locator("#link-text")).to_have_text("Connected")
    other.locator("#speech-settings").click()
    other.locator("#remote-mic-enable").click()
    expect(other.locator("#remote-mic-status")).to_contain_text("another Answer Board tab")
    expect(other.locator("#remote-mic-enable")).to_have_attribute("aria-pressed", "false")

    def remote(engine):
        payload = {"kind":"mic-toggle", "engine":engine, "id":str(uuid.uuid4()),
                   "sent_at":time.time(), "secret":"test-shared-secret"}
        assert requests.post(lan_url, json=payload).json()["mic_command"] == "queued"
        return payload

    # The receiving tab need not be focused; its microphone is used.
    payload = remote("paid")
    expect(page.locator("#mic-note")).to_contain_text("Listening")
    expect(page.locator("#mic-paid")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#mic")).to_be_enabled()
    assert requests.post(lan_url, json=payload).json()["mic_command"] == "duplicate"
    expect(page.locator("#mic-paid")).to_have_attribute("aria-pressed", "true")
    expect(other.locator("#mic-note")).to_have_text("Microphones off")
    assert other.evaluate("recognizers.length") == 0

    page.keyboard.press("Control+Alt+o")
    expect(page.locator("#mic-note")).to_have_text("Listening · Free")
    remote("free")
    expect(page.locator("#mic-note")).to_have_text("Microphones off")
    remote("free")
    expect(page.locator("#mic-note")).to_have_text("Listening · Free")
    remote("paid")
    expect(page.locator("#mic-paid")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#mic")).to_be_enabled()
    remote("paid")
    expect(page.locator("#mic-note")).to_have_text("Microphones off")
    page.locator("#speech-settings").click()
    page.locator("#remote-mic-enable").click()
    expect(page.locator("#remote-mic-enable")).to_have_attribute("aria-pressed", "false")
    assert requests.post(lan_url, json={**payload, "id":str(uuid.uuid4())}).status_code == 409
    other.close()


def test_read_only_board_loads_without_secure_context_uuid(page, board_server):
    page.add_init_script("Object.defineProperty(window.crypto, 'randomUUID', {value:undefined});")
    page.reload()
    expect(page.locator("#link-text")).to_have_text("Connected")
    board_server[0].hub.append("The LAN reader still works.")
    expect(page.locator("#committed")).to_have_text("The LAN reader still works.")


def test_board_layout(page, tmp_path):
    page.locator("#speech-settings").click()
    expect(page.locator("#speech-key")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(tmp_path / "speech-settings.png"), full_page=True)
    artifacts = Path(".test-artifacts")
    artifacts.mkdir(exist_ok=True)
    page.screenshot(path=str(artifacts / "speech-settings.png"), full_page=True)
    page.set_viewport_size({"width": 768, "height": 900})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
