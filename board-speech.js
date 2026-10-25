/* Speech engines are independent of the board renderer, so both share the
   same ordered, retryable delivery path and can be exercised without a mic. */
(function (root) {
  "use strict";

  function withTimeout(promise, ms, message) {
    var timer;
    return Promise.race([promise, new Promise(function (_, reject) {
      timer = setTimeout(function () { reject(new Error(message)); }, ms);
    })]).finally(function () { clearTimeout(timer); });
  }

  async function request(path, data) {
    var abort = new AbortController();
    var timer = setTimeout(function () { abort.abort(); }, 20000);
    try {
      var response = await fetch(path + location.search, {
        method: data === undefined ? "GET" : "POST",
        headers: { "X-Board": "1", "Content-Type": "application/json" },
        body: data === undefined ? undefined : JSON.stringify(data), signal: abort.signal
      });
      var result = await response.json();
      if (!response.ok) throw new Error(result.error || "The app could not complete this request.");
      return result;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("Request timed out. Check the app and internet connection.");
      throw error;
    } finally { clearTimeout(timer); }
  }

  function createDeliveryQueue(options) {
    var pending = options.load() || [];
    if (!Array.isArray(pending)) pending = [];
    pending = pending.filter(function (p) { return p && typeof p.id === "string" && typeof p.text === "string"; });
    var running = false, timer = null, delay = 500, lastError = "";
    function notify() { options.changed(pending.length, lastError); }
    function persist() { options.save(pending); notify(); }
    async function drain() {
      if (running) return;
      clearTimeout(timer); timer = null;
      running = true;
      try {
        while (pending.length) {
          try {
            await options.send(pending[0]);
          } catch (error) {
            lastError = "Waiting to send " + pending.length + " phrase(s). Retrying automatically.";
            notify();
            timer = setTimeout(drain, delay);
            delay = Math.min(delay * 2, 10000);
            return;
          }
          pending.shift(); delay = 500; lastError = ""; persist();
        }
      } finally { running = false; }
    }
    return {
      add: function (text) {
        text = text.trim();
        if (!text) return;
        // Stay below the server's 20 KB UTF-8 limit, including JSON escaping.
        var chars = Array.from(text);
        while (chars.length) {
          var size = Math.min(chars.length, 1500);
          if (chars.length > size) {
            for (var at = size - 1; at > 0; at--) {
              if (/\s/.test(chars[at])) { size = at + 1; break; }
            }
          }
          pending.push({id: options.id(), text: chars.splice(0, size).join("")});
        }
        persist(); drain();
      },
      retry: drain,
      count: function () { return pending.length; },
      text: function () { return pending.map(function (p) { return p.text; }).join(" "); },
      start: function () { notify(); drain(); }
    };
  }

  function createBrowserSession(options) {
    var recognition = null, wanted = true, stopping = false, retryTimer, stopTimer, errorTimer;
    var failures = 0, interim = "", stopResolve, readyResolve, readyReject;
    var ready = new Promise(function (resolve, reject) { readyResolve = resolve; readyReject = reject; });
    var FATAL = {
      "not-allowed": "Microphone permission denied. Allow it in the browser, then try again.",
      "service-not-allowed": "This browser has blocked its speech recognition service.",
      "audio-capture": "Could not capture microphone audio. Check your input device.",
      "language-not-supported": "The browser does not support this speech language."
    };
    function finish() {
      clearTimeout(retryTimer); clearTimeout(stopTimer); clearTimeout(errorTimer);
      if (stopResolve) stopResolve();
      options.onEnd();
    }
    function begin() {
      if (!wanted || stopping) return;
      var Rec = options.Recognizer;
      var mine = recognition = new Rec();
      var sent = new Set();
      mine.lang = options.language; mine.continuous = true; mine.interimResults = true;
      options.onState(failures ? "Reconnecting free mic…" : "Starting free mic…", false);
      mine.onstart = function () {
        if (mine !== recognition) return;
        readyResolve();
        options.onState("Listening · Free", true);
      };
      mine.onresult = function (event) {
        if (mine !== recognition) return;
        failures = 0;
        interim = "";
        // Rebuild ALL interim results. resultIndex only marks what changed.
        for (var i = 0; i < event.results.length; i++) {
          var result = event.results[i];
          if (result.isFinal) {
            if (!sent.has(i)) { sent.add(i); options.onFinal(result[0].transcript); }
          } else { interim += result[0].transcript; }
        }
        options.onInterim(interim);
      };
      mine.onerror = function (event) {
        if (mine !== recognition) return;
        if (FATAL[event.error]) {
          wanted = false; readyReject(new Error(FATAL[event.error]));
          options.onError(FATAL[event.error]);
          recognition = null;
          try { mine.abort(); } catch (_) { /* already ended */ }
          finish(); return;
        }
        failures++;
        options.onState(event.error === "network" ? "Speech service disconnected. Retrying…" : "Restarting free mic…", false);
        // Normally onend follows. Only recover forcibly if an actual error
        // was reported AND the browser fails to end the broken session.
        clearTimeout(errorTimer);
        errorTimer = setTimeout(function () {
          if (mine !== recognition) return;
          try { mine.abort(); } catch (_) { /* already stopped */ }
          mine.onend();
        }, 3000);
      };
      mine.onend = function () {
        if (mine !== recognition) return;
        recognition = null;
        clearTimeout(errorTimer);
        if (stopping || !wanted) { finish(); return; }
        if (interim.trim()) {
          options.onDraft(interim); interim = ""; options.onInterim("");
        }
        failures++;
        options.onState("Reconnecting free mic…", false);
        retryTimer = setTimeout(begin, Math.min(250 * Math.pow(2, failures - 1), 5000));
      };
      try { mine.start(); }
      catch (_) {
        recognition = null; wanted = false;
        readyReject(new Error("Could not start browser recognition. Check microphone permissions."));
        finish();
      }
    }
    return {
      start: function () { begin(); return withTimeout(ready, 15000, "Free mic did not start. Check browser permissions and internet access."); },
      stop: function () {
        wanted = false; stopping = true; clearTimeout(retryTimer); clearTimeout(errorTimer);
        return new Promise(function (resolve) {
          stopResolve = resolve;
          if (!recognition) { finish(); return; }
          options.onState("Finishing free transcript…", false);
          stopTimer = setTimeout(function () {
            var old = recognition; recognition = null;
            if (old) { try { old.abort(); } catch (_) { /* stopped */ } }
            finish();
          }, 8000);
          try { recognition.stop(); } catch (_) { finish(); }
        });
      }
    };
  }

  function createSpeechmaticsSession(options) {
    var socket, stream, context, source, processor, silentGain;
    var sequence = 0, capturing = false, closed = false, finishing = false;
    var readyResolve, readyReject, endResolve, flushResolve;
    var seen = new Set();
    var ready = new Promise(function (resolve, reject) { readyResolve = resolve; readyReject = reject; });
    // Install a handler immediately; failures during async microphone setup
    // must not surface as unhandled promise rejections.
    ready.catch(function () {});
    var ended = new Promise(function (resolve) { endResolve = resolve; });
    function releaseAudio() {
      capturing = false;
      if (processor) { processor.disconnect(); processor.port.onmessage = null; }
      if (source) source.disconnect();
      if (silentGain) silentGain.disconnect();
      if (stream) stream.getTracks().forEach(function (track) { track.stop(); });
      if (context && context.state !== "closed") context.close().catch(function () {});
    }
    function cleanup() {
      if (closed) return;
      closed = true; releaseAudio();
      if (socket) socket.close();
      endResolve(); options.onEnd();
    }
    function fail(message) {
      if (closed) return;
      readyReject(new Error(message)); options.onError(message); cleanup();
    }
    function sendAudio(buffer) {
      if (closed || !socket || socket.readyState !== 1) return;
      if (socket.bufferedAmount > context.sampleRate * 2 * 5) {
        fail("Connection too slow for live audio. Speechmatics stopped; reconnect when the network improves."); return;
      }
      socket.send(buffer); sequence++;
    }
    return {
      start: async function () {
        try {
          options.onState("Starting Speechmatics…", false);
          var Audio = options.AudioContext;
          context = new Audio();
          // Resume during the user's click gesture, before network awaits.
          var resumed = context.resume();
          var capture = options.getUserMedia({audio:{channelCount:1, echoCancellation:true, noiseSuppression:true}, video:false});
          capture.then(function (value) { if (closed) value.getTracks().forEach(function (t) { t.stop(); }); }, function () {});
          stream = await withTimeout(capture, 20000, "Microphone permission timed out. Allow access and try again.");
          if (closed) return;
          await withTimeout(resumed, 10000, "The browser could not start audio capture.");
          await context.audioWorklet.addModule("/speech-audio-worklet.js" + options.search);
          var auth = await options.token();
          if (closed) return;
          source = context.createMediaStreamSource(stream);
          processor = new options.AudioWorkletNode(context, "answer-board-pcm");
          silentGain = context.createGain(); silentGain.gain.value = 0;
          processor.port.onmessage = function (event) {
            if (event.data.type === "flushed") { if (flushResolve) flushResolve(); return; }
            if (capturing) sendAudio(event.data);
          };
          socket = new options.WebSocket(auth.url + "?jwt=" + encodeURIComponent(auth.token));
          socket.onopen = function () {
            if (closed) return;
            socket.send(JSON.stringify({
              message:"StartRecognition",
              audio_format:{type:"raw", encoding:"pcm_s16le", sample_rate:context.sampleRate},
              transcription_config:{language:options.language.split("-")[0], model:"enhanced", enable_partials:true,
                max_delay:1, additional_vocab:options.keywords.slice(0,100).map(function (word) { return {content:word}; })}
            }));
          };
          socket.onmessage = function (event) {
            if (closed) return;
            var msg;
            try { msg = JSON.parse(event.data); } catch (_) { fail("Speechmatics returned an unreadable response."); return; }
            if (msg.message === "RecognitionStarted") {
              capturing = true;
              source.connect(processor); processor.connect(silentGain); silentGain.connect(context.destination);
              stream.getAudioTracks().forEach(function (track) {
                track.onended = function () { if (!finishing && !closed) fail("Microphone disconnected. Reconnect it and start again."); };
              });
              options.onState("Listening · Speechmatics (paid)", true); readyResolve();
            } else if (msg.message === "AddPartialTranscript") {
              options.onInterim((msg.metadata && msg.metadata.transcript) || "");
            } else if (msg.message === "AddTranscript") {
              var meta = msg.metadata || {};
              var id = JSON.stringify([meta.start_time, meta.end_time, meta.transcript]);
              if (!seen.has(id)) { seen.add(id); if (meta.transcript) options.onFinal(meta.transcript); }
              options.onInterim("");
            } else if (msg.message === "EndOfTranscript") {
              if (!finishing) options.onError("Speechmatics ended this session. Click the paid mic to start another.");
              cleanup();
            } else if (msg.message === "Error") {
              var messages = {
                not_authorised:"Speechmatics rejected authorization. Test your saved key.",
                insufficient_funds:"Your Speechmatics account needs more credit.",
                invalid_model:"This language or model is unavailable on your Speechmatics account.",
                invalid_language:"Speechmatics does not support this selected language.",
                quota_exceeded:"Speechmatics session limit reached. Close another session and retry."
              };
              fail(messages[msg.type] || "Speechmatics could not continue. Check your account, language and connection.");
            }
          };
          socket.onerror = function () { fail("Could not connect to Speechmatics. Check internet access and your account."); };
          socket.onclose = function () {
            if (!closed) fail("Speechmatics disconnected before finishing. Finalized phrases are retained; restart the mic to continue.");
          };
          await withTimeout(ready, 30000, "Speechmatics did not start in time. Check your key, credit and connection.");
        } catch (error) {
          cleanup();
          throw new Error(error.name === "NotAllowedError" ? "Allow microphone access in your browser first." : error.message);
        }
      },
      stop: async function () {
        if (closed) return;
        finishing = true; options.onState("Finishing Speechmatics transcript…", false);
        if (!capturing || !socket || socket.readyState !== 1) { cleanup(); return; }
        try {
          await withTimeout(new Promise(function (resolve) {
            flushResolve = resolve; processor.port.postMessage({type:"finish"});
          }), 1500, "Audio flush timed out.");
          capturing = false; releaseAudio();
          socket.send(JSON.stringify({message:"EndOfStream", last_seq_no:sequence}));
          await withTimeout(ended, 12000, "Speechmatics stopped responding. The last recognized words will be added to the board.");
        } catch (error) { options.onError(error.message); }
        finally { cleanup(); }
      }
    };
  }

  function init(options) {
    var $ = options.$, session = null, active = null, busy = false, interim = "";
    var drafts = [];
    try { drafts = JSON.parse(sessionStorage.getItem("answerboard.speech-drafts.v1") || "[]"); } catch (_) { /* unavailable */ }
    if (!Array.isArray(drafts)) drafts = [];
    drafts = drafts.filter(function (text) { return typeof text === "string"; });
    var buttons = {free:$("mic"), paid:$("mic-paid")};
    var shortcuts = {free:"Ctrl + Alt + O", paid:"Ctrl + Alt + M"};
    buttons.free.setAttribute("aria-keyshortcuts", "Control+Alt+O");
    buttons.paid.setAttribute("aria-keyshortcuts", "Control+Alt+M");
    var note = $("mic-note"), errorNote = $("speech-error");
    var deliveryMessage = "Reconnecting to the board… Your text will sync automatically.";
    var lang = $("lang"), local = ["localhost","127.0.0.1","[::1]"].indexOf(location.hostname) !== -1;
    var secure = window.isSecureContext;
    var Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
    var languages = [["English (India)","en-IN"],["English (US)","en-US"],["English (UK)","en-GB"],
      ["Hindi","hi-IN"],["Telugu","te-IN"],["Tamil","ta-IN"],["Spanish","es-ES"],["French","fr-FR"],["German","de-DE"]];
    languages.forEach(function (entry) { var option=document.createElement("option"); option.value=entry[1]; option.textContent=entry[0]; lang.appendChild(option); });
    lang.value = languages.some(function (entry) { return entry[1] === options.prefs.lang; }) ? options.prefs.lang : "en-IN";
    lang.addEventListener("change", function () { options.prefs.lang = lang.value; options.savePrefs(); });
    // If an engine ends without finalizing, keep its latest recognized words
    // on the same board as all other speech. No separate recovery workflow.
    function keepDraft(text) { if (text.trim()) queue.add(text); }
    function setInterim(text) { interim = text; options.interim(text); }
    function showError(message) { errorNote.textContent = message; }
    function render(state, listening) {
      note.textContent = state;
      Object.keys(buttons).forEach(function (kind) {
        buttons[kind].classList.toggle("live", active === kind && listening);
        buttons[kind].setAttribute("aria-pressed", String(active === kind));
        buttons[kind].setAttribute("aria-label", (kind === "free" ? "Free microphone" : "Speechmatics paid microphone") + (active === kind ? " - stop" : " - start"));
        buttons[kind].disabled = busy || !secure || (kind === "free" ? !Rec : !local || !navigator.mediaDevices || !window.AudioWorkletNode);
        buttons[kind].title = active === kind ? "Stop and finish this transcript" : "Start " + (kind === "paid" ? "Speechmatics (uses your paid credit)" : "free browser recognition");
        buttons[kind].title += " (" + shortcuts[kind] + ")";
      });
      lang.disabled = Boolean(active) || busy;
    }
    var queue = createDeliveryQueue({
      load:function () { try { return JSON.parse(sessionStorage.getItem("answerboard.pending-speech.v1") || "[]"); } catch (_) { return []; } },
      save:function (pending) { try { sessionStorage.setItem("answerboard.pending-speech.v1",JSON.stringify(pending)); } catch (_) { showError("Browser storage is unavailable. Keep this tab open until your text reaches the board."); } },
      id:function () { return window.crypto.randomUUID(); },
      send:async function (phrase) {
        var abort = new AbortController(), timer = setTimeout(function () { abort.abort(); },10000);
        try {
          var response = await fetch("/append" + location.search,{method:"POST", headers:{"X-Board":"1","Content-Type":"application/json"},body:JSON.stringify(phrase),signal:abort.signal});
          if (!response.ok) throw new Error("Transcript delivery failed");
        } finally { clearTimeout(timer); }
      },
      changed:function (count,error) {
        if (error) showError(deliveryMessage);
        else if (errorNote.textContent === deliveryMessage) showError("");
      }
    });
    queue.start();
    // Upgrade older tabs: previously saved previews now join the board too.
    drafts.forEach(keepDraft);
    try { sessionStorage.removeItem("answerboard.speech-drafts.v1"); } catch (_) { /* storage unavailable */ }
    drafts = [];
    async function choose(kind) {
      if (busy) return;
      var nextKind = active === kind ? null : kind;
      busy = true; showError(""); render("Switching microphone…",false);
      try {
        if (session) await session.stop();
        session = null; active = null;
        if (!nextKind) { render("Microphones off",false); return; }
        active = nextKind;
        var mine;
        var hooks = {
          language:lang.value,
          onState:function (state,listening) { if (session === mine) render(state,listening); },
          onInterim:setInterim, onFinal:function (text) { queue.add(text); }, onError:showError, onDraft:keepDraft,
          onEnd:function () {
            if (session === mine) {
              keepDraft(interim); setInterim(""); active=null; render("Microphones off",false);
            }
          }
        };
        mine = nextKind === "free" ? createBrowserSession(Object.assign(hooks,{Recognizer:Rec})) :
          createSpeechmaticsSession(Object.assign(hooks,{
            AudioContext:window.AudioContext || window.webkitAudioContext, AudioWorkletNode:window.AudioWorkletNode,
            WebSocket:window.WebSocket,getUserMedia:navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices),
            token:function () { return request("/speech/token",{}); }, keywords:options.keywords(),search:location.search
          }));
        session = mine;
        await mine.start();
      } catch (error) {
        if (session) await session.stop();
        session=null; active=null; showError(error.message); render("Microphones off",false);
      } finally {
        busy = false;
        render(note.textContent,Boolean(active && buttons[active].classList.contains("live")));
      }
    }
    buttons.free.addEventListener("click",function () { choose("free"); });
    buttons.paid.addEventListener("click",function () { choose("paid"); });
    document.addEventListener("keydown", function (event) {
      if (!event.ctrlKey || !event.altKey || event.shiftKey || event.metaKey ||
          event.isComposing || event.getModifierState("AltGraph")) return;
      var key = event.key.toLowerCase();
      var kind = key === "o" ? "free" : key === "m" ? "paid" : null;
      if (!kind) return;
      event.preventDefault();
      // Holding a shortcut toggles only once; disabled buttons stay unavailable.
      if (!event.repeat) buttons[kind].click();
    });
    window.addEventListener("online",queue.retry);
    window.addEventListener("beforeunload",function (event) {
      if (active || queue.count() || interim.trim()) { event.preventDefault(); event.returnValue=""; }
    });
    // No visibilitychange restart: changing tabs must not abort valid speech.
    if (!secure) showError("For microphone access, open http://localhost:8766 on the laptop running the app. The LAN page can still display answers.");
    else if (!Rec) showError("Free speech recognition needs a supported browser such as Chrome or Edge. Speechmatics is available with your key.");
    render("Microphones off",false);

    var panel=$("speech-settings-panel"), key=$("speech-key"), keyStatus=$("speech-key-status");
    $("speech-settings").addEventListener("click",function () {
      panel.showModal(); $("speech-settings").setAttribute("aria-expanded","true");
      if (local) {
        request("/speech/settings").then(function (data) { keyStatus.textContent=data.message; }).catch(function (e) { keyStatus.textContent=e.message; });
      }
    });
    $("speech-settings-close").addEventListener("click",function () { panel.close(); });
    panel.addEventListener("close",function () { $("speech-settings").setAttribute("aria-expanded","false"); });
    panel.addEventListener("click",function (event) {
      if (event.target !== panel) return;
      var bounds=panel.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) panel.close();
    });
    ["save","test","remove"].forEach(function (action) {
      var button=$("speech-key-"+action); button.disabled=!local;
      button.addEventListener("click",async function () {
        var controls=["save","test","remove"].map(function (name) { return $("speech-key-"+name); });
        controls.forEach(function (el) { el.disabled=true; });
        keyStatus.textContent=action === "test" ? "Testing key…" : "Updating key…";
        try {
          var data=await request("/speech/settings",{action:action,key:action === "remove" ? undefined : key.value.trim()});
          if (action !== "test") key.value="";
          keyStatus.textContent=data.message;
        } catch (error) { keyStatus.textContent=error.message; }
        finally { controls.forEach(function (el) { el.disabled=false; }); }
      });
    });
    if (!local) { key.disabled=true; keyStatus.textContent="Manage keys and start paid transcription on this laptop's localhost board."; }
    return {hasPending:function () { return queue.count() > 0; }};
  }

  var api={init:init,createDeliveryQueue:createDeliveryQueue,createBrowserSession:createBrowserSession,createSpeechmaticsSession:createSpeechmaticsSession};
  if (typeof module !== "undefined" && module.exports) module.exports=api;
  else root.AnswerBoardSpeech=api;
})(typeof window !== "undefined" ? window : globalThis);
