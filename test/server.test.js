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
  server.exited = false;
  server.on('exit', () => (server.exited = true));
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('server did not start')), 15000);
    server.stdout.on('data', (d) => {
      if (String(d).includes('running on')) {
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
