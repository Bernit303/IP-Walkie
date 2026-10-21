// Starts the real server.js on a free port and drives it with real WebSocket
// clients. Run with: npm test
const { test, before, after, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const net = require('node:net');
const path = require('node:path');
const WebSocket = require('ws');

const TOKEN = 'test-token-0123456789abcdef';
let server;
let port;
let serverLog = ''; // everything the main test server has printed so far
const sockets = [];

function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.listen(0, '127.0.0.1', () => {
      const { port: p } = s.address();
      s.close(() => resolve(p));
    });
    s.on('error', reject);
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(fn, ms = 2000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const v = fn();
    if (v) return v;
    await sleep(25);
  }
  return fn();
}

function open() {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`wss://127.0.0.1:${port}`, { rejectUnauthorized: false });
    ws.msgs = [];
    ws.on('message', (d) => ws.msgs.push(JSON.parse(d)));
    ws.on('open', () => resolve(ws));
    ws.on('error', reject);
    sockets.push(ws);
  });
}

const last = (ws, type) => [...ws.msgs].reverse().find((m) => m.type === type);
const send = (ws, msg) => ws.send(JSON.stringify(msg));

async function client(name, token) {
  const ws = await open();
  const welcome = await waitFor(() => last(ws, 'welcome'));
  ws.id = welcome.id;
  send(ws, { type: 'join', name, token });
  await sleep(100);
  return ws;
}

before(async () => {
  port = await freePort();
  server = spawn('node', ['server.js'], {
    cwd: path.join(__dirname, '..'),
    env: { ...process.env, PORT: String(port), MAX_TRANSMIT_MS: '1000', PRIORITY_TOKEN: TOKEN, MAX_CLIENTS: '8', HEARTBEAT_MS: '300' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  server.stdout.on('data', (d) => { serverLog += d; });
  server.exited = false;
  server.on('exit', () => (server.exited = true));
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('server did not start')), 15000);
    server.stdout.on('data', (d) => {
      if (String(d).includes(' listening ')) {
        clearTimeout(timer);
        resolve();
      }
    });
    server.on('exit', () => reject(new Error('server exited during start')));
  });
});

// Each test starts with an empty room (the server is capped at MAX_CLIENTS).
afterEach(async () => {
  for (const ws of sockets.splice(0)) ws.terminate();
  await sleep(100);
});

after(() => {
  server.kill();
});

test('survives non-object and malformed frames', async () => {
  const ws = await open();
  for (const raw of ['null', '[1,2]', '"str"', '42', '{', JSON.stringify({ type: 'join', name: { toString: 1 } })]) {
    ws.send(raw);
  }
  await sleep(150);
  assert.equal(server.exited, false);
  // still works afterwards
  const c = await client('After', null);
  assert.ok(last(c, 'welcome'));
});

test('survives an invalid UTF-8 text frame', async () => {
  const ws = await open();
  const frame = WebSocket.Sender.frame(Buffer.from([0xff, 0xfe, 0xfd]), {
    fin: true, rsv1: false, opcode: 0x01, mask: true, readOnly: false,
  });
  ws._socket.write(Buffer.concat(frame));
  await sleep(200);
  assert.equal(server.exited, false);
});

test('oversized frame closes only that connection', async () => {
  const big = await open();
  const other = await client('Bystander', null);
  big.send(JSON.stringify({ type: 'offer', target: 'x', sdp: 'A'.repeat(64 * 1024) }));
  await waitFor(() => big.readyState !== WebSocket.OPEN);
  assert.notEqual(big.readyState, WebSocket.OPEN);
  assert.equal(other.readyState, WebSocket.OPEN);
});

test('names are sanitised and cannot carry the priority star', async () => {
  const a = await client('Alice', null);
  await client('⭐ Dad\u001b[2J\nX', null);
  const peers = await waitFor(() => last(a, 'peers')?.peers.find((p) => p.name.includes('Dad')));
  assert.ok(peers);
  assert.equal(peers.name, 'Dad[2JX');
  assert.equal(peers.priority, false);
});

test('unjoined connections get no broadcasts and cannot take the floor', async () => {
  const joined = await client('Joined', null);
  const lurker = await open();
  const welcome = await waitFor(() => last(lurker, 'welcome'));
  assert.ok(welcome);
  send(lurker, { type: 'ptt-start' });
  await sleep(150);
  assert.equal(last(lurker, 'floor'), undefined);
  assert.notEqual(last(joined, 'floor')?.holder, welcome.id);
});

test('floor: grant, deny, timeout, and holder cannot extend it', async () => {
  const a = await client('A', null);
  const b = await client('B', null);

  send(a, { type: 'ptt-start' });
  await waitFor(() => last(b, 'floor')?.holder === a.id);
  assert.equal(last(b, 'floor').holder, a.id);

  send(b, { type: 'ptt-start' });
  assert.ok(await waitFor(() => last(b, 'floor-denied')));

  // Re-sending ptt-start must not re-arm the 1000 ms timer.
  const t0 = Date.now();
  for (let i = 0; i < 4; i++) {
    send(a, { type: 'ptt-start' });
    await sleep(150);
  }
  await waitFor(() => last(b, 'floor')?.holder === null, 1500);
  assert.equal(last(b, 'floor').holder, null);
  assert.ok(Date.now() - t0 < 1600, 'floor should time out on schedule');
});

test('priority token preempts a normal holder; wrong token does not', async () => {
  const normal = await client('Normal', null);
  const impostor = await client('Impostor', 'wrong-token-wrong-token');
  const radio = await client('Radio', TOKEN);

  send(normal, { type: 'ptt-start' });
  await waitFor(() => last(radio, 'floor')?.holder === normal.id);

  send(impostor, { type: 'ptt-start' });
  assert.ok(await waitFor(() => last(impostor, 'floor-denied')));

  send(radio, { type: 'ptt-start' });
  assert.ok(await waitFor(() => last(normal, 'floor-preempted')));
  const floor = await waitFor(() => last(normal, 'floor')?.holder === radio.id && last(normal, 'floor'));
  assert.equal(floor.priority, true);
});

test('relay forwards only whitelisted fields and sets from/name itself', async () => {
  const a = await client('Sender', null);
  const b = await client('Receiver', null);
  send(a, { type: 'offer', target: b.id, sdp: { type: 'offer', sdp: 'v=0' }, from: 'spoofed', name: 'Spoof', extra: 'x' });
  const got = await waitFor(() => last(b, 'offer'));
  assert.equal(got.from, a.id);
  assert.equal(got.name, 'Sender');
  assert.equal(got.extra, undefined);
});

test('app-level ping gets a pong, even before join', async () => {
  const ws = await open();
  await waitFor(() => last(ws, 'welcome'));
  send(ws, { type: 'ping' });
  assert.ok(await waitFor(() => last(ws, 'pong')));
});

test('a connection that stops answering protocol pings is dropped and frees the floor', async () => {
  const watcher = await client('Watcher', null);
  const ghost = await new Promise((resolve, reject) => {
    const ws = new WebSocket(`wss://127.0.0.1:${port}`, { rejectUnauthorized: false, autoPong: false });
    ws.msgs = [];
    ws.on('message', (d) => ws.msgs.push(JSON.parse(d)));
    ws.on('open', () => resolve(ws));
    ws.on('error', reject);
    sockets.push(ws);
  });
  const welcome = await waitFor(() => last(ghost, 'welcome'));
  send(ghost, { type: 'join', name: 'Ghost' });
  send(ghost, { type: 'ptt-start' });
  await waitFor(() => last(watcher, 'floor')?.holder === welcome.id);
  assert.equal(last(watcher, 'floor').holder, welcome.id);
  // No pong from the ghost: it must be terminated and the floor released.
  await waitFor(() => last(watcher, 'floor')?.holder === null, 2500);
  assert.equal(last(watcher, 'floor').holder, null);
  await waitFor(() => ghost.readyState === WebSocket.CLOSED, 1000);
});

test('a browser that pings once then goes silent is dropped', async () => {
  const watcher = await client('Watcher2', null);
  const quiet = await client('Quiet', null);
  send(quiet, { type: 'ping' });
  await waitFor(() => last(watcher, 'peers')?.peers.some((p) => p.name === 'Quiet'));
  // Quiet keeps auto-ponging but sends nothing at the app level.
  const gone = await waitFor(() => !last(watcher, 'peers')?.peers.some((p) => p.name === 'Quiet'), 3500);
  assert.ok(gone, 'silent app-ping client should be removed from the peer list');
});

// Starts a throwaway server with extra env and resolves with its welcome message.
async function welcomeFrom(extraEnv) {
  const p = await freePort();
  const child = spawn('node', ['server.js'], {
    cwd: path.join(__dirname, '..'),
    env: { ...process.env, PORT: String(p), ...extraEnv },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  try {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('server did not start')), 15000);
      child.stdout.on('data', (d) => {
        if (String(d).includes(' listening ')) {
          clearTimeout(timer);
          resolve();
        }
      });
      child.on('exit', () => reject(new Error('server exited during start')));
    });
    return await new Promise((resolve, reject) => {
      const ws = new WebSocket(`wss://127.0.0.1:${p}`, { rejectUnauthorized: false });
      ws.on('message', (d) => {
        const m = JSON.parse(d);
        if (m.type === 'welcome') {
          ws.terminate();
          resolve(m);
        }
      });
      ws.on('error', reject);
    });
  } finally {
    child.kill();
  }
}

test('roger beep is announced as off by default', async () => {
  const ws = await open();
  const welcome = await waitFor(() => last(ws, 'welcome'));
  assert.equal(welcome.rogerBeep, false);
});

test('ROGER_BEEP turns the announcement on; junk values leave it off', async () => {
  assert.equal((await welcomeFrom({ ROGER_BEEP: 'on' })).rogerBeep, true);
  assert.equal((await welcomeFrom({ ROGER_BEEP: 'TRUE' })).rogerBeep, true);
  assert.equal((await welcomeFrom({ ROGER_BEEP: 'off' })).rogerBeep, false);
  assert.equal((await welcomeFrom({ ROGER_BEEP: 'banana' })).rogerBeep, false);
});

// ---------------------------------------------------------------------------
// Logging. These read what the real server actually printed.
// ---------------------------------------------------------------------------

const lines = (mark) => serverLog.slice(mark).split('\n').filter(Boolean);
const LINE_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z (DEBUG|INFO |WARN |ERROR) [a-z][a-z-]*( |$)/;
const has = (mark, ...needles) => lines(mark).some((l) => needles.every((n) => l.includes(n)));
const heldMs = (l) => Number((l.match(/held_ms=(\d+)/) || [])[1]);

// Starts a throwaway server with extra env; resolves once its port accepts connections.
async function startServer(extraEnv) {
  const p = await freePort();
  const child = spawn('node', ['server.js'], {
    cwd: path.join(__dirname, '..'),
    env: { ...process.env, PORT: String(p), HEARTBEAT_MS: '300', ...extraEnv },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const handle = { port: p, child, out: '' };
  child.stdout.on('data', (d) => { handle.out += d; });
  child.stderr.on('data', (d) => { handle.out += d; });
  const end = Date.now() + 15000;
  for (;;) {
    const up = await new Promise((resolve) => {
      const s = net.connect(p, '127.0.0.1', () => { s.destroy(); resolve(true); });
      s.on('error', () => resolve(false));
    });
    if (up) return handle;
    if (Date.now() > end) { child.kill(); throw new Error('server did not start'); }
    await sleep(50);
  }
}

test('startup logs the effective settings', async () => {
  assert.ok(has(0, ' INFO  config ', 'priority="enabled"', 'roger_beep="off"', 'max_clients=8', 'max_transmit_ms=1000', 'log_level="info"'));
  assert.ok(has(0, ' INFO  listening url="https://'));
});

test("the log tells a session's story: connect, join, floor, denied, release, leave", async () => {
  const mark = serverLog.length;
  const alice = await client('LogAlice', null);
  const bob = await client('LogBob', null);
  send(alice, { type: 'ptt-start' });
  await waitFor(() => last(bob, 'floor')?.holder === alice.id);
  send(bob, { type: 'ptt-start' });
  await waitFor(() => last(bob, 'floor-denied'));
  send(alice, { type: 'ptt-stop' });
  await waitFor(() => last(bob, 'floor')?.holder === null);
  bob.close();
  await waitFor(() => has(mark, ' leave ', 'name="LogBob"'));

  assert.ok(has(mark, ' INFO  connect ', 'ip="127.0.0.1"', 'clients='));
  assert.ok(has(mark, ' INFO  join ', 'name="LogAlice"', 'priority=false', 'again=false'));
  assert.ok(has(mark, ' INFO  floor-granted ', 'name="LogAlice"'));
  assert.ok(has(mark, ' INFO  floor-denied ', 'name="LogBob"', 'holder="LogAlice"'));
  const released = lines(mark).find((l) => l.includes(' floor-released ') && l.includes('name="LogAlice"'));
  assert.ok(released, "release is logged with the holder's name");
  assert.ok(released.includes('reason="ptt-stop"'));
  assert.ok(heldMs(released) >= 0);
  assert.ok(has(mark, ' INFO  leave ', 'name="LogBob"', 'reason="closed"', 'duration_s=', 'clients='));
});

test('losing the connection while talking logs the release (with the name) and then the leave', async () => {
  const mark = serverLog.length;
  const drop = await client('LogDrop', null);
  send(drop, { type: 'ptt-start' });
  await waitFor(() => has(mark, ' floor-granted ', 'name="LogDrop"'));
  drop.terminate();
  await waitFor(() => has(mark, ' leave ', 'name="LogDrop"'));
  const l = lines(mark);
  const released = l.findIndex((x) => x.includes(' floor-released ') && x.includes('name="LogDrop"') && x.includes('reason="disconnect"'));
  const left = l.findIndex((x) => x.includes(' leave ') && x.includes('name="LogDrop"'));
  assert.ok(released >= 0, 'release on disconnect is logged');
  assert.ok(left > released, 'and comes before the leave line');
});

test('a transmit timeout is logged as a warning with the limit', async () => {
  const mark = serverLog.length;
  const slow = await client('LogSlow', null);
  send(slow, { type: 'ptt-start' });
  await waitFor(() => has(mark, ' WARN  floor-timeout ', 'name="LogSlow"'), 2500);
  const l = lines(mark).find((x) => x.includes(' floor-timeout '));
  assert.ok(l && l.includes('limit_ms=1000') && heldMs(l) >= 900);
});

test('a priority preemption is logged with both names', async () => {
  const mark = serverLog.length;
  const low = await client('LogLow', null);
  const vip = await client('LogVip2', TOKEN);
  send(low, { type: 'ptt-start' });
  await waitFor(() => last(vip, 'floor')?.holder === low.id);
  send(vip, { type: 'ptt-start' });
  await waitFor(() => has(mark, ' floor-preempted ', 'by="LogVip2"', 'from="LogLow"'));
});

test('logs never contain the token, SDP, ICE or raw frames; a wrong token is reported without being shown', async () => {
  const mark = serverLog.length;
  const vip = await client('LogVip', TOKEN);
  const fake = await client('LogFake', 'wrong-token-value-1234567890');
  send(vip, { type: 'offer', target: fake.id, sdp: { type: 'offer', sdp: 'SECRET-SDP-MARKER' } });
  send(vip, { type: 'ice', target: fake.id, candidate: { candidate: 'SECRET-ICE-MARKER' } });
  const raw = await open();
  raw.send('not json SECRET-FRAME-MARKER');
  await waitFor(() => has(mark, ' invalid-frame '));
  const text = serverLog.slice(mark);
  for (const secret of [TOKEN, 'wrong-token-value', 'SECRET-SDP-MARKER', 'SECRET-ICE-MARKER', 'SECRET-FRAME-MARKER']) {
    assert.ok(!text.includes(secret), `log must not contain ${secret}`);
  }
  assert.ok(has(mark, ' WARN  priority-denied ', 'name="LogFake"', 'reason="wrong-token"'));
  assert.ok(has(mark, ' INFO  join ', 'name="LogVip"', 'priority=true'));
  assert.ok(has(mark, ' WARN  invalid-frame ', 'reason="not-json"', 'bytes='));
  for (const l of lines(mark)) assert.match(l, LINE_RE, `malformed log line: ${l}`);
});

test('a hostile name cannot add log lines or fields', async () => {
  const mark = serverLog.length;
  await client('Eve"\n2026-10-21T00:00:00.000Z INFO  floor-granted id="boss" priority=true', null);
  await waitFor(() => has(mark, ' join ', 'name="Eve'));
  for (const l of lines(mark)) assert.match(l, LINE_RE, `malformed log line: ${l}`);
  assert.ok(!has(mark, 'id="boss"'));
});

test('a burst of bad frames from one client produces one log line, not thousands', async () => {
  const mark = serverLog.length;
  const raw = await open();
  const welcome = await waitFor(() => last(raw, 'welcome'));
  for (let i = 0; i < 40; i++) raw.send('garbage ' + i);
  await waitFor(() => has(mark, ' invalid-frame '));
  await sleep(300);
  const mine = lines(mark).filter((l) => l.includes(' invalid-frame ') && l.includes(`id="${welcome.id.slice(0, 8)}"`));
  assert.equal(mine.length, 1);
});

test('flooding is logged once and the leave line says why', async () => {
  const mark = serverLog.length;
  const raw = await open();
  const welcome = await waitFor(() => last(raw, 'welcome'));
  const short = welcome.id.slice(0, 8);
  for (let i = 0; i < 800; i++) raw.send(JSON.stringify({ type: 'ping' }));
  await waitFor(() => has(mark, ' leave ', `id="${short}"`, 'reason="flood"'), 4000);
  const floods = lines(mark).filter((l) => l.includes(' WARN  flood ') && l.includes(`id="${short}"`));
  assert.equal(floods.length, 1);
  assert.ok(floods[0].includes('limit_per_s=500'));
});

test('a dead connection is logged as a warning and the leave line says why', async () => {
  const mark = serverLog.length;
  const ghost = await new Promise((resolve, reject) => {
    const ws = new WebSocket(`wss://127.0.0.1:${port}`, { rejectUnauthorized: false, autoPong: false });
    ws.msgs = [];
    ws.on('message', (d) => ws.msgs.push(JSON.parse(d)));
    ws.on('open', () => resolve(ws));
    ws.on('error', reject);
    sockets.push(ws);
  });
  await waitFor(() => last(ghost, 'welcome'));
  send(ghost, { type: 'join', name: 'LogGhost' });
  await waitFor(() => has(mark, ' WARN  dead-connection ', 'name="LogGhost"', 'check="protocol-ping"'), 3000);
  await waitFor(() => has(mark, ' leave ', 'name="LogGhost"', 'reason="dead-connection"'), 2000);
});

test('something that is not TLS on the HTTPS port is logged, and does not hurt the server', async () => {
  const mark = serverLog.length;
  const s = net.connect(port, '127.0.0.1', () => s.write('GET / HTTP/1.1\r\nHost: x\r\n\r\n'));
  s.on('error', () => {});
  await waitFor(() => has(mark, ' INFO  tls-error ', 'ip="127.0.0.1"', 'code='), 3000);
  s.destroy();
  assert.equal(server.exited, false);
});

test('json format with debug level: every line parses, relays are traced without their content', async () => {
  const s = await startServer({ LOG_FORMAT: 'json', LOG_LEVEL: 'debug', ROGER_BEEP: 'on' });
  try {
    const open2 = () => new Promise((resolve, reject) => {
      const ws = new WebSocket(`wss://127.0.0.1:${s.port}`, { rejectUnauthorized: false });
      ws.msgs = [];
      ws.on('message', (d) => ws.msgs.push(JSON.parse(d)));
      ws.on('open', () => resolve(ws));
      ws.on('error', reject);
      sockets.push(ws);
    });
    const a = await open2();
    const b = await open2();
    const wa = await waitFor(() => last(a, 'welcome'));
    const wb = await waitFor(() => last(b, 'welcome'));
    send(a, { type: 'join', name: 'JA' });
    send(b, { type: 'join', name: 'JB' });
    await sleep(150);
    send(a, { type: 'offer', target: wb.id, sdp: { type: 'offer', sdp: 'SECRET-SDP-MARKER' } });
    await waitFor(() => s.out.includes('"event":"relay"'));
    const events = s.out.split('\n').filter(Boolean).map((l) => JSON.parse(l));
    const cfg = events.find((e) => e.event === 'config');
    assert.equal(cfg.roger_beep, 'on');
    assert.equal(cfg.log_format, 'json');
    const relay = events.find((e) => e.event === 'relay');
    assert.equal(relay.level, 'debug');
    assert.equal(relay.from, wa.id.slice(0, 8));
    assert.equal(relay.to, wb.id.slice(0, 8));
    assert.ok(!s.out.includes('SECRET-SDP-MARKER'));
  } finally {
    s.child.kill();
  }
});

test('LOG_LEVEL=warn hides info lines but still shows a refused priority token', async () => {
  const s = await startServer({ LOG_LEVEL: 'warn', PRIORITY_TOKEN: 'short' });
  try {
    await sleep(300);
    assert.match(s.out, / ERROR priority-disabled /);
    assert.ok(!s.out.includes(' INFO '));
  } finally {
    s.child.kill();
  }
});

test('a bad LOG_LEVEL falls back to info and says so', async () => {
  const s = await startServer({ LOG_LEVEL: 'shouting' });
  try {
    await sleep(300);
    assert.match(s.out, / WARN {2}log-config /);
    assert.match(s.out, / INFO {2}config .*log_level="info"/);
  } finally {
    s.child.kill();
  }
});

test('the Docker image contains every local file server.js loads', () => {
  const fs = require('node:fs');
  const root = path.join(__dirname, '..');
  const dockerfile = fs.readFileSync(path.join(root, 'Dockerfile'), 'utf8');
  const copied = [...dockerfile.matchAll(/^COPY\s+(\S+)\s+\S+/gim)].map((m) => m[1].replace(/\/$/, ''));
  const needed = new Set();
  const scan = (file) => {
    const src = fs.readFileSync(path.join(root, file), 'utf8');
    for (const m of src.matchAll(/require\('(\.[^']+)'\)/g)) {
      let rel = path.normalize(path.join(path.dirname(file), m[1])).replace(/\\/g, '/');
      if (!/\.(js|json)$/.test(rel)) rel += '.js';
      if (!needed.has(rel)) {
        needed.add(rel);
        if (rel.endsWith('.js')) scan(rel);
      }
    }
  };
  scan('server.js');
  assert.ok(needed.has('lib/logger.js'), 'the scan should find the logger');
  for (const file of needed) {
    // COPY sources may use wildcards (package*.json) or name a whole directory.
    const ok = copied.some((c) => {
      const escape = (part) => part.replace(/[.+?^${}()|[\]\\]/g, '\\$&');
      const glob = new RegExp('^' + c.split('*').map(escape).join('[^/]*') + '$');
      return glob.test(file) || file.startsWith(c + '/');
    });
    assert.ok(ok, `Dockerfile does not COPY ${file}; the container would crash on start`);
  }
});

test('over the whole run, the token never appeared in the server log', () => {
  assert.ok(!serverLog.includes(TOKEN));
  assert.equal(server.exited, false);
});
