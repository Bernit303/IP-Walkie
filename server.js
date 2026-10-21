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
const { createLogger, createThrottle, createKeyedThrottle } = require('./lib/logger');

// LOG_LEVEL: debug | info (default) | warn | error.  LOG_FORMAT: text (default) | json.
// LOG_STATS_MS: how often to write a one-line summary; 0 turns it off (default 5 minutes).
const log = createLogger({
  level: (process.env.LOG_LEVEL || 'info').trim().toLowerCase(),
  format: (process.env.LOG_FORMAT || 'text').trim().toLowerCase(),
});
const LOG_STATS_MS = process.env.LOG_STATS_MS === undefined ? 300000 : Math.max(0, Number(process.env.LOG_STATS_MS) || 0);
const STARTED_AT = Date.now();
const shortId = (id) => String(id).slice(0, 8);

// A crash must leave a line in the log (the default prints a bare stack to stderr),
// and must still end the process so the container restarts, as it did before.
function logFatal(event, err) {
  log.error(event, {
    error: err && err.message,
    code: err && err.code,
    stack: String((err && err.stack) || '').split('\n').slice(0, 6).map((l) => l.trim()).join(' | '),
  });
  process.exit(1);
}
process.on('uncaughtException', (err) => logFatal('uncaught-exception', err));
process.on('unhandledRejection', (err) => logFatal('unhandled-rejection', err instanceof Error ? err : new Error(String(err))));
for (const signal of ['SIGTERM', 'SIGINT']) {
  process.on(signal, () => {
    log.info('shutdown', { signal });
    process.exit(0);
  });
}

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
// Roger beep: a short courtesy tone every client plays when the channel goes
// from "someone is talking" to free. One global switch, off unless asked for;
// the server only announces it (in `welcome`) and never touches audio.
const ROGER_BEEP = ['1', 'true', 'on', 'yes'].includes(String(process.env.ROGER_BEEP || '').trim().toLowerCase());
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
  log.error('priority-disabled', {
    reason: `PRIORITY_TOKEN is a placeholder or shorter than ${MIN_TOKEN_LENGTH} characters`,
    fix: 'generate one with: openssl rand -hex 24',
  });
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
  log.info('cert-generate', {});

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

  log.info('cert-names', { covers: sanEntries });
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

const remoteIp = (req) => String((req && req.socket && req.socket.remoteAddress) || '?').replace(/^::ffff:/, '');
// One limit per source, so a noisy device cannot hide another device's problem.
const originThrottle = createKeyedThrottle(10000);
const fullThrottle = createKeyedThrottle(10000);
const tokenThrottle = createKeyedThrottle(10000);
const tlsThrottle = createKeyedThrottle(5000);

const wss = new WebSocket.Server({
  server,
  maxPayload: MAX_MESSAGE_BYTES,
  verifyClient: ({ origin, req }) => {
    if (!ALLOWED_ORIGINS.length || !origin) return true;
    if (ALLOWED_ORIGINS.includes(origin)) return true;
    originThrottle(remoteIp(req), (suppressed) => log.warn('reject-origin', { ip: remoteIp(req), origin, suppressed }));
    return false;
  },
});

// A browser that refuses the certificate, or something probing the port without
// speaking TLS, never reaches the app; without this nothing would show it.
server.on('tlsClientError', (err, socket) => {
  tlsThrottle(remoteIp({ socket }), (suppressed) => log.info('tls-error', { ip: remoteIp({ socket }), code: err && err.code, suppressed }));
  socket.destroy();
});

const clients = new Map(); // id -> { ws, name, priority, joined }
let floorHolder = null; // id of whoever currently has permission to transmit
let floorTimer = null;
let floorSince = 0;

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
  floorSince = Date.now();
  const holder = clients.get(id);
  log.info('floor-granted', { id: shortId(id), name: holder?.name, priority: !!holder?.priority });
  broadcastFloor();
  clearTimeout(floorTimer);
  // Safety net: a frozen tab, a dead battery, or a crashed CLI client should
  // never be able to lock the channel forever.
  floorTimer = setTimeout(() => {
    if (floorHolder === id) {
      log.warn('floor-timeout', { id: shortId(id), name: clients.get(id)?.name, held_ms: Date.now() - floorSince, limit_ms: MAX_TRANSMIT_MS });
      floorHolder = null;
      broadcastFloor();
    }
  }, MAX_TRANSMIT_MS);
}

// `who` is passed on disconnect, when the client is already gone from `clients`.
function releaseFloorIfHeldBy(id, reason, who) {
  if (floorHolder === id) {
    log.info('floor-released', { id: shortId(id), name: (who || clients.get(id))?.name, reason, held_ms: Date.now() - floorSince });
    floorHolder = null;
    clearTimeout(floorTimer);
    broadcastFloor();
  }
}

const joinedCount = () => [...clients.values()].filter((c) => c.joined).length;

function broadcastPeerList() {
  const list = [...clients.entries()]
    .filter(([, c]) => c.joined)
    .map(([id, c]) => ({ id, name: c.name, priority: c.priority }));
  for (const [id, c] of clients) {
    if (!c.joined) continue;
    send(c.ws, { type: 'peers', peers: list.filter((p) => p.id !== id) });
  }
}

wss.on('connection', (ws, req) => {
  if (clients.size >= MAX_CLIENTS) {
    fullThrottle(remoteIp(req), (suppressed) => log.warn('reject-full', { ip: remoteIp(req), max_clients: MAX_CLIENTS, suppressed }));
    ws.close(1013, 'server full');
    return;
  }

  const id = crypto.randomBytes(8).toString('hex');
  const me = {
    ws, name: 'Anon', priority: false, joined: false, alive: true, appPing: false, lastMsg: Date.now(),
    connectedAt: Date.now(), endReason: null, ip: remoteIp(req),
    // Per-connection limits on log lines a misbehaving client could repeat; freed with the connection.
    frameThrottle: createThrottle(5000), deniedThrottle: createThrottle(1000), errorThrottle: createThrottle(1000),
  };
  clients.set(id, me);
  log.info('connect', {
    id: shortId(id), ip: me.ip, origin: req.headers.origin, ua: req.headers['user-agent'], clients: clients.size,
  });

  // Without a listener, an 'error' event (invalid UTF-8, oversize frame,
  // reset) is thrown by EventEmitter and takes the whole process down.
  ws.on('error', (err) => {
    me.endReason = me.endReason || 'socket-error';
    me.errorThrottle((suppressed) => log.warn('socket-error', { id: shortId(id), code: err && err.code, error: err && err.message, suppressed }));
    ws.terminate();
  });
  ws.on('pong', () => { me.alive = true; });

  send(ws, { type: 'welcome', id, rogerBeep: ROGER_BEEP });

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
      const again = me.joined;
      me.name = cleanName(msg.name);
      me.priority = tokenMatches(msg.token);
      me.joined = true;
      // Never the token itself, only that one was offered and why it did not count.
      if (typeof msg.token === 'string' && msg.token && !me.priority) {
        tokenThrottle(`${me.ip}|${me.name}`, (suppressed) =>
          log.warn('priority-denied', { id: shortId(id), name: me.name, reason: PRIORITY_DIGEST ? 'wrong-token' : 'priority-disabled-on-server', suppressed }));
      }
      log.info('join', { id: shortId(id), name: me.name, priority: me.priority, again, clients: joinedCount() });
      broadcastPeerList();
      return;
    }

    if (!me.joined) {
      log.debug('ignored-before-join', { id: shortId(id), type: msg.type });
      return;
    }

    if (msg.type === 'offer' || msg.type === 'answer' || msg.type === 'ice') {
      if (typeof msg.target !== 'string') return;
      const target = clients.get(msg.target);
      if (target && target.joined && msg.target !== id) {
        log.debug('relay', { type: msg.type, from: shortId(id), to: shortId(msg.target) });
        // Whitelist the relayed fields; `from` and `name` are always ours.
        send(target.ws, {
          type: msg.type,
          target: msg.target,
          sdp: msg.sdp,
          candidate: msg.candidate,
          from: id,
          name: me.name,
        });
      } else {
        log.debug('relay-dropped', { type: msg.type, from: shortId(id), to: shortId(msg.target) });
      }
      return;
    }

    if (msg.type === 'ptt-start') {
      // Already the holder: nothing to do. Re-granting would re-arm the
      // transmit timer and let a client keep the floor forever.
      if (floorHolder === id) {
        log.debug('ptt-start-ignored', { id: shortId(id), reason: 'already-holder' });
        return;
      }

      const holder = floorHolder ? clients.get(floorHolder) : null;
      if (floorHolder === null) {
        grantFloor(id);
      } else if (me.priority && !holder?.priority) {
        log.info('floor-preempted', { by_id: shortId(id), by: me.name, from_id: shortId(floorHolder), from: holder?.name });
        if (holder) send(holder.ws, { type: 'floor-preempted', by: me.name });
        grantFloor(id);
      } else {
        send(ws, { type: 'floor-denied', holder: floorHolder, name: holder?.name });
        me.deniedThrottle((suppressed) =>
          log.info('floor-denied', { id: shortId(id), name: me.name, holder_id: shortId(floorHolder), holder: holder?.name, suppressed }));
      }
      return;
    }

    if (msg.type === 'ptt-stop') {
      if (floorHolder === id) releaseFloorIfHeldBy(id, 'ptt-stop', me);
      else log.debug('ptt-stop-ignored', { id: shortId(id), reason: 'not-holder' });
      return;
    }

    log.debug('unknown-message', { id: shortId(id), type: msg.type });
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
      if (!me.endReason) {
        me.endReason = 'flood';
        log.warn('flood', { id: shortId(id), name: me.name, limit_per_s: MAX_MESSAGES_PER_SECOND });
      }
      ws.terminate();
      return;
    }

    let msg;
    // Only the size and kind of a bad frame are logged, never its content.
    const bad = (reason) =>
      me.frameThrottle((suppressed) => log.warn('invalid-frame', { id: shortId(id), reason, bytes: raw.length, suppressed }));
    try {
      msg = JSON.parse(raw);
    } catch {
      bad('not-json');
      return;
    }
    // JSON.parse('null') is valid JSON; so are arrays and bare strings.
    if (!msg || typeof msg !== 'object' || Array.isArray(msg)) {
      bad('not-an-object');
      return;
    }

    try {
      handle(msg);
    } catch (err) {
      // Drop the bad message, keep the process (and everyone else's channel).
      me.errorThrottle((suppressed) =>
        log.error('message-error', { id: shortId(id), type: msg.type, error: err && err.message, suppressed }));
    }
  });

  ws.on('close', (code) => {
    clients.delete(id);
    releaseFloorIfHeldBy(id, 'disconnect', me);
    broadcastPeerList();
    log.info('leave', {
      id: shortId(id), name: me.joined ? me.name : undefined, joined: me.joined,
      reason: me.endReason || 'closed', code, duration_s: Math.round((Date.now() - me.connectedAt) / 1000), clients: clients.size,
    });
  });
});

const heartbeat = setInterval(() => {
  const now = Date.now();
  for (const [id, c] of clients) {
    // Browsers ping at the application level, so judge them on that;
    // everyone else on protocol-level pongs.
    const stale = c.appPing ? now - c.lastMsg > 2 * HEARTBEAT_MS + 1000 : !c.alive;
    if (stale) {
      c.endReason = 'dead-connection';
      log.warn('dead-connection', { id: shortId(id), name: c.joined ? c.name : undefined, check: c.appPing ? 'app-ping' : 'protocol-ping', silent_s: Math.round((now - c.lastMsg) / 1000) });
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
  log.info('config', {
    version: require('./package.json').version,
    node: process.version,
    pid: process.pid,
    port: PORT,
    max_clients: MAX_CLIENTS,
    max_transmit_ms: MAX_TRANSMIT_MS,
    heartbeat_ms: HEARTBEAT_MS,
    priority: PRIORITY_TOKEN ? 'enabled' : 'disabled',
    roger_beep: ROGER_BEEP ? 'on' : 'off',
    allowed_origins: ALLOWED_ORIGINS.length,
    log_level: log.level,
    log_format: log.format,
    stats_ms: LOG_STATS_MS,
  });
  log.info('listening', { url: `https://${lanIp}:${PORT}` });
  if (LOG_STATS_MS > 0) {
    const stats = setInterval(() => {
      log.info('stats', {
        clients: clients.size,
        joined: joinedCount(),
        floor: floorHolder ? clients.get(floorHolder)?.name || 'unknown' : 'idle',
        uptime_s: Math.round((Date.now() - STARTED_AT) / 1000),
        rss_mb: Math.round(process.memoryUsage().rss / 1048576),
      });
    }, LOG_STATS_MS);
    stats.unref();
  }
});
