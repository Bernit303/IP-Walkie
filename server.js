// Express reads NODE_ENV at require time; default to production so error
// responses (e.g. a bad Range header on a static file) never carry stack traces.
if (!process.env.NODE_ENV) process.env.NODE_ENV = 'production';

const express = require('express');
const https = require('https');
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const WebSocket = require('ws');
const { execFileSync } = require('child_process');

const PORT = Number(process.env.PORT) || 8443;
const CERT_DIR = path.join(__dirname, 'certs');
const KEY_PATH = path.join(CERT_DIR, 'key.pem');
const CERT_PATH = path.join(CERT_DIR, 'cert.pem');

const MAX_TRANSMIT_MS = Number(process.env.MAX_TRANSMIT_MS) || 60000;
const MAX_CLIENTS = Number(process.env.MAX_CLIENTS) || 32;
const MAX_MESSAGE_BYTES = 32 * 1024; // SDP offers are a few KB; nothing legitimate is near this
const MAX_MESSAGES_PER_SECOND = 500; // flood guard, well above a burst of ICE candidates
const NAME_MAX_CHARS = 24;
// Dead-connection detection. A phone that roams between access points can
// vanish without closing its socket; without this it would hold the floor
// until MAX_TRANSMIT_MS and stay in everyone's peer list.
const HEARTBEAT_MS = Number(process.env.HEARTBEAT_MS) || 5000;
const MIN_TOKEN_LENGTH = 16;
const PLACEHOLDER_TOKENS = new Set(['some-long-random-string', 'changeme', 'change-me', 'secret']);

// Optional: comma-separated list of origins (e.g. https://walkie.example.ts.net)
// allowed to open the WebSocket from a browser. Unset = no Origin check, the
// previous behavior. Non-browser clients (the CLI) send no Origin and are
// always allowed.
const ALLOWED_ORIGINS = (process.env.ALLOWED_ORIGINS || '').split(',').map((s) => s.trim()).filter(Boolean);

// Whoever joins with this token is marked priority and can preempt a
// non-priority speaker. Leave unset to disable priority entirely.
let PRIORITY_TOKEN = process.env.PRIORITY_TOKEN || null;
if (PRIORITY_TOKEN && (PRIORITY_TOKEN.length < MIN_TOKEN_LENGTH || PLACEHOLDER_TOKENS.has(PRIORITY_TOKEN.toLowerCase()))) {
  console.error(
    `PRIORITY_TOKEN is a placeholder or shorter than ${MIN_TOKEN_LENGTH} characters — priority is DISABLED. ` +
      'Generate one with: openssl rand -hex 24'
  );
  PRIORITY_TOKEN = null;
}
const PRIORITY_DIGEST = PRIORITY_TOKEN ? sha256(PRIORITY_TOKEN) : null;

function sha256(s) {
  return crypto.createHash('sha256').update(s).digest();
}

function tokenMatches(candidate) {
  if (!PRIORITY_DIGEST || typeof candidate !== 'string') return false;
  return crypto.timingSafeEqual(sha256(candidate), PRIORITY_DIGEST);
}

function detectLocalAddresses() {
  const ips = new Set(['127.0.0.1']);
  const nets = os.networkInterfaces();
  for (const iface of Object.values(nets)) {
    for (const addr of iface || []) {
      if (addr.family === 'IPv4' && !addr.internal) ips.add(addr.address);
    }
  }
  return [...ips];
}

function ensureCert() {
  if (fs.existsSync(KEY_PATH) && fs.existsSync(CERT_PATH)) return;
  fs.mkdirSync(CERT_DIR, { recursive: true });
  console.log('Generating self-signed certificate...');

  // Autodetection sees whatever network namespace this process runs in.
  // Inside Docker that's the container's internal identity (e.g. 172.18.0.2),
  // not the LAN address people actually visit — so let it be overridden.
  const extraHosts = (process.env.CERT_HOSTNAMES || '').split(',').map((s) => s.trim()).filter(Boolean);
  const extraIps = (process.env.CERT_IPS || '').split(',').map((s) => s.trim()).filter(Boolean);

  const hostname = os.hostname();
  const ips = new Set([...detectLocalAddresses(), ...extraIps]);
  const sanEntries = [
    'DNS:localhost',
    `DNS:${hostname}`,
    ...extraHosts.map((h) => `DNS:${h}`),
    ...[...ips].map((ip) => `IP:${ip}`),
  ].join(',');

  console.log(`Certificate will cover: ${sanEntries}`);
  // execFileSync, not execSync: hostnames and CERT_* values go in as argv
  // entries and never touch a shell.
  execFileSync('openssl', [
    'req', '-x509', '-newkey', 'rsa:2048',
    '-keyout', KEY_PATH, '-out', CERT_PATH,
    '-days', '3650', '-nodes',
    '-subj', `/CN=${hostname}`,
    '-addext', `subjectAltName=${sanEntries}`,
  ]);
  try { fs.chmodSync(KEY_PATH, 0o600); } catch { /* best effort (Windows) */ }
}

// Display names are peer-supplied and end up in other users' UIs, terminals
// and our own logs. Slice by code point first, then strip control, format,
// surrogate and line/paragraph-separator characters and the priority star,
// so a name can neither forge the priority marker nor smuggle escapes.
function cleanName(value) {
  if (typeof value !== 'string') return 'Anon';
  const sliced = Array.from(value).slice(0, NAME_MAX_CHARS).join('');
  const cleaned = sliced.replace(/[\p{C}\p{Zl}\p{Zp}⭐]/gu, '').trim();
  return cleaned || 'Anon';
}

ensureCert();

const app = express();
app.disable('x-powered-by');
app.use((req, res, next) => {
  res.set({
    'Content-Security-Policy':
      "default-src 'self'; connect-src 'self' wss:; media-src 'self' blob: mediastream:; " +
      "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'no-referrer',
  });
  next();
});
app.use(express.static(path.join(__dirname, 'public')));

const server = https.createServer(
  {
    key: fs.readFileSync(KEY_PATH),
    cert: fs.readFileSync(CERT_PATH),
  },
  app
);

const wss = new WebSocket.Server({
  server,
  maxPayload: MAX_MESSAGE_BYTES,
  verifyClient: ({ origin }) => {
    if (!ALLOWED_ORIGINS.length || !origin) return true;
    return ALLOWED_ORIGINS.includes(origin);
  },
});

const clients = new Map(); // id -> { ws, name, priority, joined }
let floorHolder = null; // id of whoever currently has permission to transmit
let floorTimer = null;

function send(ws, msg) {
  if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

function broadcastFloor() {
  const holder = floorHolder ? clients.get(floorHolder) : null;
  for (const [, c] of clients) {
    if (!c.joined) continue;
    send(c.ws, { type: 'floor', holder: floorHolder, name: holder?.name || null, priority: !!holder?.priority });
  }
}

function grantFloor(id) {
  floorHolder = id;
  broadcastFloor();
  clearTimeout(floorTimer);
  // Safety net: a frozen tab, a dead battery, or a crashed CLI client should
  // never be able to lock the channel forever.
  floorTimer = setTimeout(() => {
    if (floorHolder === id) {
      console.log(`Transmit timeout — releasing floor held by ${JSON.stringify(clients.get(id)?.name || id)}`);
      floorHolder = null;
      broadcastFloor();
    }
  }, MAX_TRANSMIT_MS);
}

function releaseFloorIfHeldBy(id) {
  if (floorHolder === id) {
    floorHolder = null;
    clearTimeout(floorTimer);
    broadcastFloor();
  }
}

function broadcastPeerList() {
  const list = [...clients.entries()]
    .filter(([, c]) => c.joined)
    .map(([id, c]) => ({ id, name: c.name, priority: c.priority }));
  for (const [id, c] of clients) {
    if (!c.joined) continue;
    send(c.ws, { type: 'peers', peers: list.filter((p) => p.id !== id) });
  }
}

wss.on('connection', (ws) => {
  if (clients.size >= MAX_CLIENTS) {
    ws.close(1013, 'server full');
    return;
  }

  const id = crypto.randomBytes(8).toString('hex');
  const me = { ws, name: 'Anon', priority: false, joined: false, alive: true, appPing: false, lastMsg: Date.now() };
  clients.set(id, me);

  // Without a listener, an 'error' event (invalid UTF-8, oversize frame,
  // reset) is thrown by EventEmitter and takes the whole process down.
  ws.on('error', () => ws.terminate());
  ws.on('pong', () => { me.alive = true; });

  send(ws, { type: 'welcome', id });

  let windowStart = Date.now();
  let windowCount = 0;

  function handle(msg) {
    // Application-level liveness. Needed because a reverse proxy (e.g.
    // Tailscale Serve) answers protocol pings itself, which would hide a dead
    // phone behind it. Browsers send this every few seconds.
    if (msg.type === 'ping') {
      me.appPing = true;
      send(ws, { type: 'pong' });
      return;
    }

    if (msg.type === 'join') {
      me.name = cleanName(msg.name);
      me.priority = tokenMatches(msg.token);
      me.joined = true;
      broadcastPeerList();
      return;
    }

    if (!me.joined) return;

    if (msg.type === 'offer' || msg.type === 'answer' || msg.type === 'ice') {
      if (typeof msg.target !== 'string') return;
      const target = clients.get(msg.target);
      if (target && target.joined && msg.target !== id) {
        // Whitelist the relayed fields; `from` and `name` are always ours.
        send(target.ws, {
          type: msg.type,
          target: msg.target,
          sdp: msg.sdp,
          candidate: msg.candidate,
          from: id,
          name: me.name,
        });
      }
      return;
    }

    if (msg.type === 'ptt-start') {
      // Already the holder: nothing to do. Re-granting would re-arm the
      // transmit timer and let a client keep the floor forever.
      if (floorHolder === id) return;

      const holder = floorHolder ? clients.get(floorHolder) : null;
      if (floorHolder === null) {
        grantFloor(id);
      } else if (me.priority && !holder?.priority) {
        console.log(`${JSON.stringify(me.name)} (priority) preempting ${JSON.stringify(holder?.name)}`);
        if (holder) send(holder.ws, { type: 'floor-preempted', by: me.name });
        grantFloor(id);
      } else {
        send(ws, { type: 'floor-denied', holder: floorHolder, name: holder?.name });
      }
      return;
    }

    if (msg.type === 'ptt-stop') {
      releaseFloorIfHeldBy(id);
    }
  }

  ws.on('message', (raw) => {
    const now = Date.now();
    me.alive = true;
    me.lastMsg = now;
    if (now - windowStart >= 1000) {
      windowStart = now;
      windowCount = 0;
    }
    if (++windowCount > MAX_MESSAGES_PER_SECOND) {
      ws.terminate();
      return;
    }

    let msg;
    try {
      msg = JSON.parse(raw);
    } catch {
      return;
    }
    // JSON.parse('null') is valid JSON; so are arrays and bare strings.
    if (!msg || typeof msg !== 'object' || Array.isArray(msg)) return;

    try {
      handle(msg);
    } catch (err) {
      // Drop the bad message, keep the process (and everyone else's channel).
      console.error('Error handling message:', err && err.message);
    }
  });

  ws.on('close', () => {
    clients.delete(id);
    releaseFloorIfHeldBy(id);
    broadcastPeerList();
  });
});

const heartbeat = setInterval(() => {
  const now = Date.now();
  for (const [, c] of clients) {
    // Browsers ping at the application level, so judge them on that;
    // everyone else on protocol-level pongs.
    const stale = c.appPing ? now - c.lastMsg > 2 * HEARTBEAT_MS + 1000 : !c.alive;
    if (stale) {
      c.ws.terminate();
      continue;
    }
    c.alive = false;
    try { c.ws.ping(); } catch { /* socket already going away */ }
  }
}, HEARTBEAT_MS);
heartbeat.unref();

server.listen(PORT, '0.0.0.0', () => {
  const lanIp = detectLocalAddresses().find((ip) => ip !== '127.0.0.1') || 'localhost';
  console.log(`LAN Walkie running on https://${lanIp}:${PORT}`);
});
