let ws;
let myId;
let myName;
let localStream;
let audioTrack;
let floorHolder = null; // id of whoever currently holds the channel
let wantTalk = false; // PTT physically held right now (set before the server grants)
let lastMsgAt = 0; // last time anything arrived from the server
const peers = new Map(); // id -> RTCPeerConnection
const audioEls = new Map();
let reconnectTimer = null;
let backoff = 1000;

const pttBtn = document.getElementById('ptt');
const statusEl = document.getElementById('status');
const peerListEl = document.getElementById('peer-list');
const nameInput = document.getElementById('name-input');
const joinBtn = document.getElementById('join-btn');
const setupEl = document.getElementById('setup');
const talkEl = document.getElementById('talk');

const config = { iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] };

// Roger beep. The server decides (ROGER_BEEP) and announces it in `welcome`;
// off until told otherwise. Played locally with Web Audio when the channel
// goes from someone talking to free, so it never touches the voice path.
//
// A MOTOTRBO-style talk-permit warble: four back-to-back notes (G6 C6 G6 E6,
// 200 ms in all). cli/lan_walkie_cli.py plays the same notes (BEEP_PATTERN);
// change both together.
const BEEP_PATTERN = [
  { freq: 1568, ms: 80 },
  { freq: 1046.5, ms: 40 },
  { freq: 1568, ms: 40 },
  { freq: 1318.5, ms: 40 },
];
const BEEP_LEVEL = 0.7;
let rogerBeep = false;
let beepCtx = null;
const beepHintEl = document.getElementById('beep-hint');

// Shown while the beep is on but the browser is still blocking sound: without
// this a phone that only listens would stay silent and nobody would know why.
function updateBeepHint() {
  const locked = rogerBeep && (!beepCtx || beepCtx.state !== 'running');
  beepHintEl.classList.toggle('hidden', !locked);
}

// Browsers only allow audio after a user gesture, and a returning visitor
// auto-joins without one, so unlock on the first touch/click/key instead.
function unlockBeepAudio() {
  try {
    if (!beepCtx) {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      beepCtx = new Ctx();
      beepCtx.onstatechange = updateBeepHint;
    }
    if (beepCtx.state !== 'running') beepCtx.resume().then(updateBeepHint, () => {});
    updateBeepHint();
  } catch {}
}
for (const evt of ['pointerdown', 'touchend', 'keydown', 'click']) {
  window.addEventListener(evt, unlockBeepAudio, { passive: true });
}

function playRogerBeep() {
  try {
    if (!beepCtx || beepCtx.state !== 'running') return; // not unlocked yet: stay silent
    // One oscillator for the whole warble, so the phase stays continuous when
    // the pitch jumps (separate oscillators would click at every change).
    // Fundamental plus a little third harmonic gives it a small speaker's bite.
    const wave = beepCtx.createPeriodicWave(new Float32Array(4), new Float32Array([0, 0.93, 0, 0.07]));
    const osc = beepCtx.createOscillator();
    osc.setPeriodicWave(wave);
    const env = beepCtx.createGain();
    // 300-3400 Hz band, like a handheld's speaker path.
    const hp = beepCtx.createBiquadFilter();
    hp.type = 'highpass';
    hp.frequency.value = 300;
    const lp = beepCtx.createBiquadFilter();
    lp.type = 'lowpass';
    lp.frequency.value = 3400;
    const out = beepCtx.createGain();
    out.gain.value = BEEP_LEVEL;
    osc.connect(env).connect(hp).connect(lp).connect(out).connect(beepCtx.destination);

    const start = beepCtx.currentTime + 0.01;
    let t = start;
    for (const note of BEEP_PATTERN) {
      osc.frequency.setValueAtTime(note.freq, t);
      t += note.ms / 1000;
    }
    // 2 ms fades at each end so the warble doesn't click.
    env.gain.setValueAtTime(0, start);
    env.gain.linearRampToValueAtTime(1, start + 0.002);
    env.gain.setValueAtTime(1, t - 0.002);
    env.gain.linearRampToValueAtTime(0, t);
    osc.start(start);
    osc.stop(t + 0.02);
  } catch {}
}

// The floor is exclusive — you never hear your own remote peers while
// transmitting, so there's no full-duplex/echo scenario here. Browser echo
// cancellation/noise suppression/AGC add real processing latency for a
// benefit this app structurally doesn't need; turn them off.
const audioConstraints = {
  echoCancellation: false,
  noiseSuppression: false,
  autoGainControl: false,
};

const NAME_KEY = 'walkie-name';
const myNameLabel = document.getElementById('my-name-label');
const changeNameLink = document.getElementById('change-name');

joinBtn.addEventListener('click', () => join(nameInput.value.trim()));
nameInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') join(nameInput.value.trim()); });
changeNameLink.addEventListener('click', (e) => {
  e.preventDefault();
  localStorage.removeItem(NAME_KEY);
  location.reload();
});

async function join(name) {
  myName = name || 'Anon';
  try {
    localStream = await navigator.mediaDevices.getUserMedia({ audio: audioConstraints });
  } catch (e) {
    alert('Microphone access denied. Allow the mic in your browser settings and reload.');
    return;
  }
  localStorage.setItem(NAME_KEY, myName);
  audioTrack = localStream.getAudioTracks()[0];
  audioTrack.enabled = false; // muted until PTT is held
  setupEl.classList.add('hidden');
  talkEl.classList.remove('hidden');
  myNameLabel.textContent = myName;
  connect();
}

// Remembered from a previous visit — names rarely change on a given device,
// so skip straight past the name prompt instead of asking every time.
const savedName = localStorage.getItem(NAME_KEY);
if (savedName) {
  join(savedName);
}

// Sends only if the current socket is open; a socket we just abandoned must
// not throw into WebRTC event handlers.
function wsSend(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

function connect() {
  // Events from a socket we've already abandoned (a half-dead one after a
  // Wi-Fi roam) must never touch current state, so every handler checks that
  // it still belongs to the live socket.
  const sock = new WebSocket(`wss://${location.host}`);
  ws = sock;
  lastMsgAt = Date.now();
  pttBtn.disabled = true;

  sock.addEventListener('open', () => {
    if (sock !== ws) return;
    statusEl.textContent = 'Connected';
    pttBtn.disabled = false;
    backoff = 1000;
  });
  sock.addEventListener('close', () => {
    if (sock === ws) scheduleReconnect();
  });

  sock.addEventListener('message', async (event) => {
    if (sock !== ws) return;
    lastMsgAt = Date.now();
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }
    if (!msg || typeof msg !== 'object') return;

    if (msg.type === 'pong') return;

    if (msg.type === 'welcome') {
      myId = msg.id;
      rogerBeep = msg.rogerBeep === true;
      updateBeepHint();
      wsSend({ type: 'join', name: myName });
      return;
    }

    if (msg.type === 'peers') {
      const incomingIds = new Set(msg.peers.map((p) => p.id));
      for (const p of msg.peers) {
        if (!peers.has(p.id)) createPeer(p.id, myId < p.id);
      }
      for (const id of [...peers.keys()]) {
        if (!incomingIds.has(id)) removePeer(id);
      }
      renderPeerList(msg.peers);
      return;
    }

    if (msg.type === 'offer') {
      // A second offer from a peer we already negotiated with means it
      // rebuilt its connection (e.g. after a Wi-Fi roam). Its new connection
      // has a new DTLS identity, so start fresh instead of renegotiating.
      const existing = peers.get(msg.from);
      if (existing && existing.remoteDescription) removePeer(msg.from);
      const pc = peers.get(msg.from) || createPeer(msg.from, false);
      await pc.setRemoteDescription(msg.sdp);
      const answer = await pc.createAnswer();
      await pc.setLocalDescription(answer);
      wsSend({ type: 'answer', target: msg.from, sdp: pc.localDescription });
      return;
    }

    if (msg.type === 'answer') {
      const pc = peers.get(msg.from);
      if (pc) await pc.setRemoteDescription(msg.sdp);
      return;
    }

    if (msg.type === 'ice') {
      const pc = peers.get(msg.from);
      if (pc && msg.candidate) {
        try { await pc.addIceCandidate(msg.candidate); } catch {}
      }
      return;
    }

    if (msg.type === 'floor') {
      const previousHolder = floorHolder;
      floorHolder = msg.holder;
      if (floorHolder === myId) {
        if (!wantTalk) {
          // The button was released before the grant arrived. Give the
          // floor straight back instead of holding an open mic until the
          // server's transmit timeout.
          audioTrack.enabled = false;
          wsSend({ type: 'ptt-stop' });
          return;
        }
        audioTrack.enabled = true;
        pttBtn.classList.add('active');
        setPttLabel('TALKING');
      } else if (floorHolder) {
        audioTrack.enabled = false;
        pttBtn.classList.remove('active');
        setPttLabel(`${msg.name} IS TALKING`, !!msg.priority);
      } else {
        audioTrack.enabled = false;
        pttBtn.classList.remove('active');
        setPttLabel('HOLD TO TALK');
        // Someone was talking and now nobody is. A hand-over to another
        // speaker (priority preemption) is not "free", so no beep there.
        if (rogerBeep && previousHolder) playRogerBeep();
      }
      return;
    }

    if (msg.type === 'floor-denied') {
      setPttLabel(`${msg.name} IS TALKING`);
      return;
    }

    if (msg.type === 'floor-preempted') {
      statusEl.textContent = '';
      statusEl.append('Interrupted by ', priorityMark(), String(msg.by));
      setTimeout(() => { statusEl.textContent = 'Connected'; }, 3000);
      return;
    }
  });
}

// A phone that changes access point often keeps a TCP connection that looks
// open but carries nothing; the browser can take minutes to notice. Ping the
// server and treat silence as a dead connection.
const HEARTBEAT_MS = 4000;
const DEAD_AFTER_MS = 12000;

function dropSocketAndReconnect() {
  const dead = ws;
  ws = null; // handlers of the abandoned socket now ignore themselves
  if (dead) {
    try { dead.close(); } catch {}
  }
  scheduleReconnect();
}

setInterval(() => {
  if (!ws || reconnectTimer) return;
  if (ws.readyState === WebSocket.OPEN) {
    if (Date.now() - lastMsgAt > DEAD_AFTER_MS) {
      dropSocketAndReconnect();
    } else {
      try { wsSend({ type: 'ping' }); } catch {}
    }
  } else if (ws.readyState === WebSocket.CONNECTING && Date.now() - lastMsgAt > DEAD_AFTER_MS) {
    dropSocketAndReconnect();
  }
}, HEARTBEAT_MS);

// Coming back online / back to the foreground: check right away instead of
// waiting for the next heartbeat.
function checkNow() {
  if (!myName || reconnectTimer) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  lastMsgAt = Math.min(lastMsgAt, Date.now() - DEAD_AFTER_MS + 2000);
}
window.addEventListener('online', checkNow);
document.addEventListener('visibilitychange', () => { if (!document.hidden) checkNow(); });

// Wi-Fi roaming between rooms/access points looks exactly like a dropped
// connection to the browser — the previous behavior ("Disconnected — reload
// to rejoin") required a human to notice and act, which doesn't work for
// non-technical users. Reconnect automatically instead, same
// backoff shape as the CLI's connection_loop() (1s → 2s → 4s... capped at
// 30s, reset to 1s on success).
function scheduleReconnect() {
  if (reconnectTimer) return;
  statusEl.textContent = 'Reconnecting…';
  pttBtn.disabled = true;
  resetForReconnect();
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, backoff);
  backoff = Math.min(backoff * 2, 30000);
}

// The server hands out a fresh id and peer list on rejoin — anything tied
// to the old connection (peer ids, in-flight WebRTC connections, floor
// state) is meaningless once it's gone and must not linger into the next
// session.
function resetForReconnect() {
  for (const id of [...peers.keys()]) removePeer(id);
  floorHolder = null;
  wantTalk = false;
  if (audioTrack) audioTrack.enabled = false;
  pttBtn.classList.remove('active');
  setPttLabel('HOLD TO TALK');
}

// The priority marker is always a separate element, never a prefix inside
// the name string: peers choose their own names, so an in-band "⭐ " would be
// indistinguishable from the real marker. The server also strips ⭐ from names.
function priorityMark() {
  const span = document.createElement('span');
  span.className = 'priority-mark';
  span.textContent = '⭐ ';
  return span;
}

function setPttLabel(text, priority = false) {
  pttBtn.textContent = '';
  if (priority) pttBtn.append(priorityMark());
  pttBtn.append(text);
}

function createPeer(id, initiator) {
  const pc = new RTCPeerConnection(config);
  peers.set(id, pc);

  localStream.getTracks().forEach((track) => pc.addTrack(track, localStream));

  pc.addEventListener('track', (event) => {
    let audioEl = audioEls.get(id);
    if (!audioEl) {
      audioEl = new Audio();
      audioEl.autoplay = true;
      audioEls.set(id, audioEl);
    }
    audioEl.srcObject = event.streams[0];

    // Chrome-only, silently ignored elsewhere: shrinks the jitter buffer's
    // target delay. Worth trading a little resilience to network jitter for
    // lower mouth-to-ear latency on a LAN.
    try { event.receiver.playoutDelayHint = 0; } catch {}
  });

  pc.addEventListener('icecandidate', (event) => {
    if (event.candidate) {
      wsSend({ type: 'ice', target: id, candidate: event.candidate });
    }
  });

  // A broken Wi-Fi path (a roam between access points, mid-call) shows up
  // here as 'failed' — with no recovery, that peer just stays silently dead
  // until someone reloads. Rebuild it instead. Deliberately not reacting to
  // 'disconnected': browsers routinely recover from that on their own (it's
  // often just a brief packet-loss blip) and escalate to 'failed' themselves
  // if it doesn't resolve — acting earlier would tear down connections that
  // were about to heal on their own.
  //
  // Roaming update: 'disconnected' that doesn't heal within a few seconds is
  // treated like 'failed'. Waiting for the browser to escalate on its own
  // left phones silent for far too long after switching access points.
  let disconnectTimer = null;
  function recover() {
    if (peers.get(id) !== pc) return; // already replaced
    removePeer(id);
    if (myId < id) {
      createPeer(id, true); // negotiationneeded fires a fresh offer automatically
    }
    // Non-initiator side just waits for that new offer, same as first
    // contact — see the 'offer' handler in connect().
  }
  pc.addEventListener('iceconnectionstatechange', () => {
    clearTimeout(disconnectTimer);
    if (pc.iceConnectionState === 'failed') {
      recover();
    } else if (pc.iceConnectionState === 'disconnected') {
      disconnectTimer = setTimeout(recover, 4000);
    }
  });

  if (initiator) {
    pc.addEventListener('negotiationneeded', async () => {
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      wsSend({ type: 'offer', target: id, sdp: pc.localDescription });
    });
  }

  return pc;
}

function removePeer(id) {
  const pc = peers.get(id);
  if (pc) pc.close();
  peers.delete(id);
  const audioEl = audioEls.get(id);
  if (audioEl) audioEl.srcObject = null;
  audioEls.delete(id);
}

function renderPeerList(list) {
  peerListEl.innerHTML = '';
  if (list.length === 0) {
    peerListEl.innerHTML = '<li class="empty">No one else here yet</li>';
    return;
  }
  for (const p of list) {
    const li = document.createElement('li');
    if (p.priority) li.appendChild(priorityMark());
    li.append(String(p.name));
    peerListEl.appendChild(li);
  }
}

function startTalk() {
  if (!audioTrack || floorHolder || !ws || ws.readyState !== WebSocket.OPEN) return;
  wantTalk = true;
  wsSend({ type: 'ptt-start' });
}

function stopTalk() {
  wantTalk = false;
  if (!audioTrack || floorHolder !== myId || !ws || ws.readyState !== WebSocket.OPEN) return;
  wsSend({ type: 'ptt-stop' });
}

pttBtn.addEventListener('pointerdown', startTalk);
pttBtn.addEventListener('pointerup', stopTalk);
pttBtn.addEventListener('pointercancel', stopTalk);
pttBtn.addEventListener('pointerleave', stopTalk);
pttBtn.addEventListener('lostpointercapture', stopTalk);

// Mobile browsers treat a held-down touch as a long-press gesture and pop up
// their own share/save/copy menu, which steals the touch — that fires
// pointercancel mid-transmission and looks like PTT randomly cutting out
// after about a second. CSS (touch-action, -webkit-touch-callout) heads most
// of this off; this is the last-resort backstop for whatever slips through.
pttBtn.addEventListener('contextmenu', (e) => e.preventDefault());

// Defensive releases: if the tab is backgrounded, the device sleeps, or the
// window loses focus mid-transmission, don't leave the channel locked.
window.addEventListener('blur', stopTalk);
window.addEventListener('pagehide', stopTalk);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) stopTalk();
});

window.addEventListener('keydown', (e) => { if (e.code === 'Space' && !e.repeat) startTalk(); });
window.addEventListener('keyup', (e) => { if (e.code === 'Space') stopTalk(); });
