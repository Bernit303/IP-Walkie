let ws;
let myId;
let myName;
let localStream;
let audioTrack;
let floorHolder = null; // id of whoever currently holds the channel
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

// Remembered from a previous visit — this is a family device, names don't
// change often, so skip straight past the name prompt instead of asking
// every time.
const savedName = localStorage.getItem(NAME_KEY);
if (savedName) {
  join(savedName);
}

function connect() {
  ws = new WebSocket(`wss://${location.host}`);
  pttBtn.disabled = true;

  ws.addEventListener('open', () => {
    statusEl.textContent = 'Connected';
    pttBtn.disabled = false;
    backoff = 1000;
  });
  ws.addEventListener('close', scheduleReconnect);

  ws.addEventListener('message', async (event) => {
    const msg = JSON.parse(event.data);

    if (msg.type === 'welcome') {
      myId = msg.id;
      ws.send(JSON.stringify({ type: 'join', name: myName }));
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
      const pc = peers.get(msg.from) || createPeer(msg.from, false);
      await pc.setRemoteDescription(msg.sdp);
      const answer = await pc.createAnswer();
      await pc.setLocalDescription(answer);
      ws.send(JSON.stringify({ type: 'answer', target: msg.from, sdp: pc.localDescription }));
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
      floorHolder = msg.holder;
      if (floorHolder === myId) {
        audioTrack.enabled = true;
        pttBtn.classList.add('active');
        pttBtn.textContent = 'TALKING';
      } else if (floorHolder) {
        audioTrack.enabled = false;
        pttBtn.classList.remove('active');
        pttBtn.textContent = msg.priority ? `⭐ ${msg.name} IS TALKING` : `${msg.name} IS TALKING`;
      } else {
        audioTrack.enabled = false;
        pttBtn.classList.remove('active');
        pttBtn.textContent = 'HOLD TO TALK';
      }
      return;
    }

    if (msg.type === 'floor-denied') {
      pttBtn.textContent = `${msg.name} IS TALKING`;
      return;
    }

    if (msg.type === 'floor-preempted') {
      statusEl.textContent = `Interrupted by ⭐ ${msg.by}`;
      setTimeout(() => { statusEl.textContent = 'Connected'; }, 3000);
      return;
    }
  });
}

// Wi-Fi roaming between rooms/access points looks exactly like a dropped
// connection to the browser — the previous behavior ("Disconnected — reload
// to rejoin") required a human to notice and act, which doesn't work for
// parents who won't know to do that. Reconnect automatically instead, same
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
  if (audioTrack) audioTrack.enabled = false;
  pttBtn.classList.remove('active');
  pttBtn.textContent = 'HOLD TO TALK';
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
      ws.send(JSON.stringify({ type: 'ice', target: id, candidate: event.candidate }));
    }
  });

  // A broken Wi-Fi path (a roam between access points, mid-call) shows up
  // here as 'failed' — with no recovery, that peer just stays silently dead
  // until someone reloads. Rebuild it instead. Deliberately not reacting to
  // 'disconnected': browsers routinely recover from that on their own (it's
  // often just a brief packet-loss blip) and escalate to 'failed' themselves
  // if it doesn't resolve — acting earlier would tear down connections that
  // were about to heal on their own.
  pc.addEventListener('iceconnectionstatechange', () => {
    if (pc.iceConnectionState === 'failed') {
      removePeer(id);
      if (myId < id) {
        createPeer(id, true); // negotiationneeded fires a fresh offer automatically
      }
      // Non-initiator side just waits for that new offer, same as first
      // contact — see the 'offer' handler in connect().
    }
  });

  if (initiator) {
    pc.addEventListener('negotiationneeded', async () => {
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      ws.send(JSON.stringify({ type: 'offer', target: id, sdp: pc.localDescription }));
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
    li.textContent = p.priority ? `⭐ ${p.name}` : p.name;
    peerListEl.appendChild(li);
  }
}

function startTalk() {
  if (!audioTrack || floorHolder || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: 'ptt-start' }));
}

function stopTalk() {
  if (!audioTrack || floorHolder !== myId || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: 'ptt-stop' }));
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
