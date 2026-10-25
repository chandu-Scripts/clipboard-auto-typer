"""Real Chromium UI/audio tests, with fake microphone audio and mocked providers.

No real API key, paid service, clipboard monitor, Word, or microphone is used.
"""
import json
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright, expect

import speech_config
from test_speech import board_server, credentials  # shared isolated fixtures


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


def test_switch_to_paid_streams_pcm_and_finishes_before_free(page, monkeypatch):
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
    page.locator("#mic").click()
    expect(page.locator("#mic-note")).to_have_text("Listening · Free")
    page.evaluate("recognizers[0].tail='free final'")
    page.locator("#mic-paid").click()
    expect(page.locator("#mic-note")).to_have_text("Listening · Speechmatics (paid)")
    expect(page.locator("#committed")).to_have_text("free final")
    expect(page.locator("#interim")).to_have_text("paid preview")
    assert messages[0]["audio_format"]["encoding"] == "pcm_s16le"
    assert messages[0]["transcription_config"]["model"] == "enhanced"
    # Actual AudioWorklet / fake-device capture, not a mocked PCM encoder.
    page.wait_for_timeout(250)
    page.locator("#mic").click()
    expect(page.locator("#mic-note")).to_have_text("Listening · Free")
    expect(page.locator("#committed")).to_have_text("free final paid final")
    assert audio
    assert messages[-1] == {"message":"EndOfStream", "last_seq_no":len(audio)}
    assert all(len(chunk) % 2 == 0 for chunk in audio)
    assert page.evaluate("recognizers.length") == 2
    page.locator("#mic").click()


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
