const express = require('express');
const https = require('https');
const fs = require('fs');
const os = require('os');
const path = require('path');
const WebSocket = require('ws');
const { execSync } = require('child_process');

const PORT = process.env.PORT || 8443;
const CERT_DIR = path.join(__dirname, 'certs');
const KEY_PATH = path.join(CERT_DIR, 'key.pem');
const CERT_PATH = path.join(CERT_DIR, 'cert.pem');

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
  execSync(
    `openssl req -x509 -newkey rsa:2048 -keyout ${KEY_PATH} -out ${CERT_PATH} -days 3650 -nodes ` +
      `-subj "/CN=${hostname}" -addext "subjectAltName=${sanEntries}"`
  );
}

ensureCert();

const app = express();
app.use(express.static(path.join(__dirname, 'public')));

const server = https.createServer(
  {
    key: fs.readFileSync(KEY_PATH),
    cert: fs.readFileSync(CERT_PATH),
  },
  app
);

const wss = new WebSocket.Server({ server });
const clients = new Map(); // id -> { ws, name, priority }
let floorHolder = null; // id of whoever currently has permission to transmit
let floorTimer = null;
const MAX_TRANSMIT_MS = Number(process.env.MAX_TRANSMIT_MS) || 60000;
// Whoever joins with this token is marked priority and can preempt a
// non-priority speaker. Leave unset to disable priority entirely.
const PRIORITY_TOKEN = process.env.PRIORITY_TOKEN || null;

function send(ws, msg) {
  if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

function broadcastFloor() {
  const holder = floorHolder ? clients.get(floorHolder) : null;
  for (const [, c] of clients) {
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
      console.log(`Transmit timeout — releasing floor held by ${clients.get(id)?.name || id}`);
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
  const list = [...clients.entries()].map(([id, c]) => ({ id, name: c.name, priority: c.priority }));
  for (const [id, c] of clients) {
    send(c.ws, { type: 'peers', peers: list.filter((p) => p.id !== id) });
  }
}

wss.on('connection', (ws) => {
  const id = Math.random().toString(36).slice(2, 10);
  clients.set(id, { ws, name: 'Anon', priority: false });
  send(ws, { type: 'welcome', id });

  ws.on('message', (raw) => {
    let msg;
    try {
      msg = JSON.parse(raw);
    } catch {
      return;
    }

    if (msg.type === 'join') {
      const client = clients.get(id);
      client.name = String(msg.name || 'Anon').slice(0, 24);
      client.priority = !!(PRIORITY_TOKEN && msg.token === PRIORITY_TOKEN);
      broadcastPeerList();
      return;
    }

    if (msg.type === 'offer' || msg.type === 'answer' || msg.type === 'ice') {
      const target = clients.get(msg.target);
      if (target) send(target.ws, { ...msg, from: id, name: clients.get(id).name });
      return;
    }

    if (msg.type === 'ptt-start') {
      const requester = clients.get(id);
      const holder = floorHolder ? clients.get(floorHolder) : null;

      if (floorHolder === null || floorHolder === id) {
        grantFloor(id);
      } else if (requester.priority && !holder?.priority) {
        console.log(`${requester.name} (priority) preempting ${holder?.name}`);
        send(holder.ws, { type: 'floor-preempted', by: requester.name });
        grantFloor(id);
      } else {
        send(requester.ws, { type: 'floor-denied', holder: floorHolder, name: holder?.name });
      }
      return;
    }

    if (msg.type === 'ptt-stop') {
      releaseFloorIfHeldBy(id);
      return;
    }
  });

  ws.on('close', () => {
    clients.delete(id);
    releaseFloorIfHeldBy(id);
    broadcastPeerList();
  });
});

server.listen(PORT, '0.0.0.0', () => {
  const lanIp = detectLocalAddresses().find((ip) => ip !== '127.0.0.1') || 'localhost';
  console.log(`LAN Walkie running on https://${lanIp}:${PORT}`);
});
