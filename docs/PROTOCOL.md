# Signaling protocol

WebSocket, JSON messages, one connection per participant. `server.js` is
the source of truth for behavior; `public/app.js` (browser) and
`cli/lan_walkie_cli.py` (headless) both implement this same protocol.
Changing a message shape means updating all three.

Every message has a `type` field. Fields below are the ones each type
actually uses.

## Connection lifecycle

### `welcome` (server → client)

Sent immediately on connect.

```json
{ "type": "welcome", "id": "lt7qe9dv" }
```

`id` is a random string, generated per-connection, used as this
participant's identity for the rest of the session.

### `join` (client → server)

Client's first message, right after `welcome`.

```json
{ "type": "join", "name": "Alice", "token": null }
```

- `name` — display name. Must be a string (anything else becomes `"Anon"`).
  Server-side: cut to 24 code points, then control/format/surrogate
  characters, line/paragraph separators and `⭐` are removed, then trimmed;
  empty becomes `"Anon"`. Clients render the priority marker from the
  `priority` boolean, never from the name.
- `token` — optional string. If it matches the server's `PRIORITY_TOKEN`
  (compared in constant time), this client is marked `priority: true`
  server-side for the rest of the session. Browser clients never send this
  field; the CLI sends `null` unless a token was supplied *and* the server's
  TLS identity was verified.

Triggers a `peers` broadcast to every joined client.

**Limits and ordering.** Until `join`, a connection receives no broadcasts
and its `offer`/`answer`/`ice`/`ptt-*` messages are ignored. Messages over
32 KiB close that connection; more than 500 messages/second closes it; a
server at `MAX_CLIENTS` (32) closes new connections with code 1013. Frames
that aren't a JSON object are ignored.

### `peers` (server → client)

Sent to each client whenever the roster changes (someone joins, changes
name, or disconnects). Each client gets everyone *except themselves*.

```json
{
  "type": "peers",
  "peers": [
    { "id": "qlyl4aki", "name": "Radio", "priority": true }
  ]
}
```

Receiving this is what triggers WebRTC connection setup — see "Mesh setup"
below.

## Mesh setup (WebRTC signaling relay)

The server does nothing with these except forward them to the named
`target` (a joined peer other than the sender). Only `type`, `target`, `sdp`
and `candidate` are forwarded, plus server-set `from` and the sender's
current `name`; any other fields in the message are dropped.

### `offer` / `answer` (client → server → target client)

```json
{
  "type": "offer",
  "target": "qlyl4aki",
  "sdp": { "type": "offer", "sdp": "v=0\r\n..." }
}
```

Server relays as:

```json
{
  "type": "offer",
  "target": "qlyl4aki",
  "sdp": { "type": "offer", "sdp": "v=0\r\n..." },
  "from": "lt7qe9dv",
  "name": "Alice"
}
```

`answer` is identical, just `sdp.type` is `"answer"`.

### `ice` (client → server → target client)

```json
{
  "type": "ice",
  "target": "qlyl4aki",
  "candidate": { "candidate": "candidate:...", "sdpMLineIndex": 0 }
}
```

Relayed the same way, with `from`/`name` added.

**Who initiates:** whichever peer has the lexicographically smaller id
sends the offer (`myId < peerId`). Both browser and CLI use this exact
rule — it doesn't matter which side "goes first" as long as both
implementations agree, and they do.

## Floor control (who's allowed to transmit)

### `ptt-start` (client → server)

```json
{ "type": "ptt-start" }
```

No payload — the server knows who sent it from the connection. Server
logic:

0. Requester already holds the floor → ignored. (Re-granting would re-arm
   the transmit timer and let a holder keep the floor indefinitely.)
1. Floor is free → grant it (see `floor` below), start the
   transmit-timeout timer.
2. Floor is held by someone else, requester has a valid priority token,
   and the current holder does **not** have priority → preempt. Previous
   holder gets `floor-preempted`. Requester gets the floor.
3. Otherwise → requester gets `floor-denied`.

### `ptt-stop` (client → server)

```json
{ "type": "ptt-stop" }
```

If the sender currently holds the floor, releases it (see `floor` below).
No-op otherwise. Also called implicitly by the server on disconnect
(`ws.on('close')`), so a dropped connection while transmitting still
releases the floor.

### `floor` (server → all clients, broadcast)

Sent to every connected client whenever the floor holder changes —
granted, released, preempted, or auto-released by timeout.

```json
{ "type": "floor", "holder": "lt7qe9dv", "name": "Alice", "priority": false }
```

`holder` is `null` when the channel is free. Every client's job on
receiving this:

- `holder === myId` → unmute local mic, show "talking" state.
- `holder` is someone else → mute local mic (if not already), show
  "`name` is talking" (with a ⭐ prefix if `priority` is true).
- `holder === null` → mute, show "ready to talk".

Two client rules that exist because of real races:

- If you received the grant but the PTT button is already released (grant
  arrived after release), send `ptt-stop` immediately — don't sit on an open
  mic until the server timeout.
- A `floor` broadcast naming someone else must not clear your *pending*
  request. Only `floor-denied`, `floor-preempted`, or losing a floor you
  actually held ends your intent to talk.

The AIOC gateway additionally keys the radio on a `floor` naming someone
else, and unkeys when it is `null` or names the gateway itself
(`docs/AIOC.md`).

### `floor-denied` (server → requester only)

```json
{ "type": "floor-denied", "holder": "lt7qe9dv", "name": "Alice" }
```

Sent only to whoever's `ptt-start` was rejected. Client must reset any
local "I'm transmitting" flag here — this was an actual bug in the CLI
(fixed): the flag wasn't reset on denial, so the *next* press would
incorrectly try to send `ptt-stop` first.

### `floor-preempted` (server → previous holder only)

```json
{ "type": "floor-preempted", "by": "Radio" }
```

Sent only to whoever just got bumped by a priority client. Purely
informational — the accompanying `floor` broadcast already updates their
mute state and UI; this just explains *why* it changed. Client shows a
brief "interrupted by ⭐ `by`" notice.

## Message types NOT implemented

Nothing else exists on the wire. In particular there's no `leave` message
— disconnection is detected via the WebSocket closing (`ws.on('close')`
server-side), which triggers `releaseFloorIfHeldBy` and a `peers`
broadcast automatically.
