import queue
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

import main
from remote_mic import RemoteMicControls


def command(engine="free", identifier="command-1"):
    return {"kind": "mic-toggle", "engine": engine, "id": identifier, "sent_at": time.time()}


def test_only_enabled_local_tab_receives_commands_and_duplicates_are_ignored():
    controls = RemoteMicControls()
    first, second = queue.Queue(), queue.Queue()
    controls.connect("first", first)
    controls.connect("second", second)
    controls.configure("first", True)
    with pytest.raises(ValueError, match="another"):
        controls.configure("second", True)
    payload = command()
    assert controls.submit(payload) == {"mic_command": "queued"}
    assert controls.submit(payload) == {"mic_command": "duplicate"}
    event, data = first.get_nowait()
    assert event == "mic-command" and data["engine"] == "free"
    assert first.empty() and second.empty()
    controls.disconnect(first)
    with pytest.raises(ValueError, match="enable remote"):
        controls.submit(command(identifier="next"))
    controls.configure("second", True)
    controls.submit(command("paid", "last"))
    assert second.get_nowait()[1]["engine"] == "paid"


@pytest.mark.parametrize("changes", [
    {"engine": "unknown"}, {"id": None}, {"id": []}, {"sent_at": 0},
    {"sent_at": float("nan")}, {"sent_at": time.time() + 1000},
])
def test_invalid_or_stale_commands_cannot_start_a_mic(changes):
    controls = RemoteMicControls()
    events = queue.Queue()
    controls.connect("local", events)
    controls.configure("local", True)
    with pytest.raises(ValueError):
        controls.submit({**command(), **changes})
    assert events.empty()


@pytest.fixture
def lan_mic_receiver(board_server):
    board, _ = board_server
    app = SimpleNamespace(board=board.hub, paused=False, remote_watch_enabled=True,
                          remote_config={"secret": "test-shared-secret"},
                          mode_var=SimpleNamespace(get=lambda: "type"), on_status=Mock())
    app.receive_mic_command = lambda payload: main.App.receive_mic_command(app, payload)
    server = main.LANServer(("127.0.0.1", 0), main.LANRequestHandler)
    server.app_ref = app
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield app, "http://127.0.0.1:" + str(server.server_address[1])
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_lan_requires_secret_and_receiver_enabled(lan_mic_receiver):
    app, url = lan_mic_receiver
    events = queue.Queue()
    app.board.remote_mic.connect("local", events)
    app.board.remote_mic.configure("local", True)
    payload = {**command(), "secret": "wrong"}
    assert requests.post(url, json=payload).status_code == 403
    assert events.empty()
    payload["secret"] = "test-shared-secret"
    app.remote_watch_enabled = False
    assert requests.post(url, json=payload).status_code == 409
    assert events.empty()
    app.remote_watch_enabled = True
    assert requests.post(url, json=payload).json()["mic_command"] == "queued"
    assert requests.post(url, json=payload).json()["mic_command"] == "duplicate"
    assert events.qsize() == 1


@pytest.mark.parametrize("transport", ["lan", "internet"])
def test_sender_uses_selected_transport_and_one_ordered_queue(transport):
    app = SimpleNamespace(
        mode_var=SimpleNamespace(get=lambda: "send"),
        connection_type_var=SimpleNamespace(get=lambda: transport),
        lan_receiver_ip_var=SimpleNamespace(get=lambda: "192.168.1.15"),
        remote_config={"secret": "test-secret", "topic": "test-topic", "lan_port": 8765},
        send_clipboard_remote_enabled=True, mic_commands=queue.Queue(), on_status=Mock())
    for engine in ("paid", "free", "paid"):
        main.App.send_mic_hotkey(app, engine)
    records = [app.mic_commands.get_nowait() for _ in range(3)]
    assert [record[2]["engine"] for record in records] == ["paid", "free", "paid"]
    assert len({record[2]["id"] for record in records}) == 3
    for url, relay, payload in records:
        assert url == ("http://192.168.1.15:8765/" if transport == "lan" else main.NTFY_BASE_URL + "/test-topic")
        assert relay == (transport == "internet")
        assert payload["secret"] == "test-secret"


def test_sender_hotkeys_are_removed_in_receiver_mode(monkeypatch):
    add = Mock(side_effect=lambda *args, **kwargs: Mock())
    monkeypatch.setattr(main.keyboard, "add_hotkey", add)
    app = SimpleNamespace(mic_hotkeys=[], root=Mock(), send_mic_hotkey=Mock())
    main.App.configure_mic_hotkeys(app, "send")
    assert [call.args[0] for call in add.call_args_list] == ["ctrl+alt+o", "ctrl+alt+m"]
    for call in add.call_args_list:
        assert call.kwargs["suppress"] and call.kwargs["trigger_on_release"]
    removers = list(app.mic_hotkeys)
    main.App.configure_mic_hotkeys(app, "type")
    assert not app.mic_hotkeys
    for remove in removers:
        remove.assert_called_once()


def test_remote_control_setup_requires_local_origin_and_connected_tab(board_server):
    board, url = board_server
    payload = {"client": "test-tab", "enabled": True}
    assert requests.post(url + "/speech/control", json=payload).status_code == 404
    headers = {"X-Board": "1", "Origin": "http://untrusted.example"}
    assert requests.post(url + "/speech/control", json=payload, headers=headers).status_code == 403
    headers["Origin"] = url
    assert requests.post(url + "/speech/control", json=payload, headers=headers).status_code == 409
    events = queue.Queue()
    board.hub.remote_mic.connect("test-tab", events)
    assert requests.post(url + "/speech/control", json=payload, headers=headers).json() == {"enabled": True}
    handler = object.__new__(main.BoardRequestHandler)
    handler.client_address = ("192.168.1.22", 1234)
    handler._json_reply = Mock()
    assert not handler._speech_local_only()


def test_relay_checks_secret_and_deduplicates_mic_commands(monkeypatch):
    class EndTest(BaseException):
        pass

    app = SimpleNamespace(remote_watch_enabled=True,
        connection_type_var=SimpleNamespace(get=lambda: "internet"),
        mode_var=SimpleNamespace(get=lambda: "type"),
        remote_config={"secret": "test-secret", "topic": "test-topic"},
        board=main.BoardHub(), on_status=Mock())
    app.receive_mic_command = lambda payload: main.App.receive_mic_command(app, payload)
    events = queue.Queue()
    app.board.remote_mic.connect("local", events)
    app.board.remote_mic.configure("local", True)
    good = {**command("paid"), "secret": "test-secret"}
    def lines(**kwargs):
        for payload in ({**good, "secret": "wrong"}, good, good):
            yield "data: " + json.dumps({"event": "message", "message": json.dumps(payload)})
        raise EndTest()
    response = Mock()
    response.iter_lines = lines
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(main.requests, "get", Mock(return_value=response))
    monkeypatch.setattr(main.pythoncom, "CoInitialize", lambda: None)
    with pytest.raises(EndTest):
        main.App.remote_watch_loop(app)
    assert events.qsize() == 1
    assert events.get_nowait()[1]["engine"] == "paid"


# Reuse the isolated HTTP server without launching the desktop application.
from test_speech import board_server
