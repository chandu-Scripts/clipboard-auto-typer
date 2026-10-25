import json
import threading
from unittest.mock import Mock

import pytest
import requests

import main
import speech_config


@pytest.fixture
def credentials(tmp_path, monkeypatch):
    path = tmp_path / "speech_credentials.json"
    monkeypatch.setattr(speech_config, "CONFIG_PATH", path)
    return path


def test_key_encrypted_persistent_and_removed(credentials):
    assert not speech_config.key_status()["configured"]
    speech_config.save_key("a-test-key-not-a-real-credential")
    assert "a-test-key" not in credentials.read_text()
    assert speech_config.load_key() == "a-test-key-not-a-real-credential"
    assert speech_config.key_status()["configured"]
    assert "encrypted_key" in json.loads(credentials.read_text())
    speech_config.remove_key()
    assert not credentials.exists()
    assert not speech_config.key_status()["configured"]


def test_bad_key_does_not_replace_existing(credentials):
    speech_config.save_key("original-test-key")
    before = credentials.read_bytes()
    for key in (None, "", "key\r\nHeader: injected", "a" * 4097):
        with pytest.raises(speech_config.SpeechConfigError):
            speech_config.save_key(key)
    assert credentials.read_bytes() == before


def test_corrupt_key_is_safe_and_replaceable(credentials):
    credentials.write_text('{"encrypted_key":"broken"}')
    assert not speech_config.key_status()["configured"]
    speech_config.save_key("replacement-test-key")
    assert speech_config.load_key() == "replacement-test-key"


def response(status=200, payload=None):
    value = Mock(status_code=status)
    value.json.return_value = payload or {"key_value": "temporary-test-token"}
    value.__enter__ = Mock(return_value=value)
    value.__exit__ = Mock(return_value=None)
    return value


def test_temporary_token_only_and_fixed_endpoint(monkeypatch):
    post = Mock(return_value=response())
    monkeypatch.setattr(speech_config.requests, "post", post)
    assert speech_config.create_token("private-test-key") == {
        "token": "temporary-test-token", "url": speech_config.REALTIME_URL,
    }
    assert post.call_args.args == (speech_config.TOKEN_URL,)
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer private-test-key"
    assert post.call_args.kwargs["json"] == {"ttl": 60}
    assert not post.call_args.kwargs["allow_redirects"]


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
def test_token_errors_do_not_expose_key(monkeypatch, status):
    post = Mock(return_value=response(status, {"error": "private-test-key"}))
    monkeypatch.setattr(speech_config.requests, "post", post)
    with pytest.raises(speech_config.SpeechConfigError) as error:
        speech_config.create_token("private-test-key")
    assert "private-test-key" not in str(error.value)


def test_network_error_is_redacted(monkeypatch):
    monkeypatch.setattr(speech_config.requests, "post", Mock(side_effect=requests.ConnectionError("private-test-key")))
    with pytest.raises(speech_config.SpeechConfigError) as error:
        speech_config.create_token("private-test-key")
    assert "private-test-key" not in str(error.value)


@pytest.fixture
def board_server():
    server = main.BoardServer(("127.0.0.1", 0), main.BoardRequestHandler)
    server.hub = main.BoardHub()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, "http://127.0.0.1:" + str(server.server_address[1])
    server.stopping.set()
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_phrase_retries_commit_once_even_after_clear(board_server):
    server, url = board_server
    args = dict(json={"id": "test:1", "text": "hello"}, headers={"X-Board": "1"})
    assert requests.post(url + "/append", **args).status_code == 204
    assert requests.post(url + "/append", **args).status_code == 204
    assert server.hub.subscribe()[1]["text"] == "hello"
    requests.post(url + "/clear", headers={"X-Board": "1"})
    requests.post(url + "/append", **args)
    assert server.hub.subscribe()[1]["text"] == ""


def test_key_routes_require_same_origin_and_header(board_server, credentials):
    _, url = board_server
    body = {"action": "save", "key": "local-test-key"}
    assert requests.post(url + "/speech/settings", json=body).status_code == 404
    assert requests.post(url + "/speech/settings", json=body,
                         headers={"X-Board": "1", "Origin": "http://untrusted.example"}).status_code == 403
    assert not credentials.exists()
    assert requests.post(url + "/speech/settings", json=body,
                         headers={"X-Board": "1", "Origin": url}).status_code == 200
    status = requests.get(url + "/speech/settings").json()
    assert status["configured"]
    assert "local-test-key" not in json.dumps(status)


def test_lan_cannot_manage_key_or_request_token():
    handler = object.__new__(main.BoardRequestHandler)
    handler.client_address = ("192.168.1.22", 12345)
    handler._json_reply = Mock()
    assert not handler._speech_local_only()
    assert handler._json_reply.call_args.args[0] == 403


def test_static_assets_and_csp(board_server):
    _, url = board_server
    page = requests.get(url)
    assert "wss://eu.rt.speechmatics.com" in page.headers["Content-Security-Policy"]
    assert "worker-src 'self'" in page.headers["Content-Security-Policy"]
    for asset in ("board-speech.js", "speech-audio-worklet.js"):
        result = requests.get(url + "/" + asset)
        assert result.status_code == 200
        assert result.headers["Content-Type"].startswith("application/javascript")
    assert requests.get(url + "/speech_credentials.json").status_code == 404


@pytest.mark.parametrize("payload", [[], {"text": 123}, {"text": "hi", "id": []}])
def test_invalid_transcripts_rejected(board_server, payload):
    _, url = board_server
    assert requests.post(url + "/append", json=payload, headers={"X-Board": "1"}).status_code == 400
