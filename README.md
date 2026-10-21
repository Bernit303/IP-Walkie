# LAN Walkie

A self-hosted push-to-talk walkie-talkie for your own network. Open a web
page, hold a button, talk. Everyone else on the channel hears you. A headless
client lets machines without a browser join too, including a **radio gateway**
that bridges a handheld radio to the channel with an
[AIOC](https://github.com/skuep/AIOC) USB cable.

- No accounts, no cloud, no media server: audio goes peer-to-peer over WebRTC.
- One speaker at a time, with a server-side timeout so a stuck client can't
  lock the channel.
- Optional priority node that can interrupt the current speaker.
- Browser client is plain HTML/CSS/JS with no build step.

> **Status: early, hobby-grade, but working end to end.** The browser mesh, the
> server and the AIOC radio gateway have all been run on real hardware
> (Ubuntu 24.04, an AIOC, a Baofeng handheld, Chrome and Firefox peers) and have
> automated tests. It has not had a multi-day soak test yet. See
> [Project status](#project-status).

## How it works

```
   browser ◄──────── WebRTC audio (direct, encrypted) ────────► browser
      │                                                            │
      └──────────────┐                              ┌──────────────┘
                     ▼                              ▼
              ┌──────────────────────────────────────────┐
              │ server.js — HTTPS + WebSocket signaling  │   never sees audio
              │ SDP/ICE relay, who-holds-the-floor       │
              └──────────────────────────────────────────┘
                     ▲
      headless CLI ──┘          AIOC ◄──► handheld radio (gateway mode)
```

Every participant keeps a WebSocket to the server and a direct WebRTC
connection to every other participant (a full mesh). The server only relays
the connection setup and decides who may transmit. That is plenty for a
household or small group; past roughly 6–8 simultaneous participants you would
want a media server (SFU) instead.

## Quick start

### Docker

```bash
git clone https://github.com/Bernit303/IP-Walkie.git
cd IP-Walkie
# edit docker-compose.yml: set CERT_HOSTNAMES / CERT_IPS to how you reach the host
docker compose up -d --build
```

### Node

Requires Node.js 20+ and `openssl` on the path.

```bash
npm ci
npm start
# LAN Walkie running on https://<your-ip>:8443
```

Open `https://<your-ip>:8443` on any device on the network, accept the
self-signed-certificate warning once, enter a name, and hold the button.
(Browsers refuse microphone access over plain HTTP outside `localhost`, which
is why the server is HTTPS-only.) To avoid the warning entirely, put it behind
[Tailscale Serve](docs/DEPLOYMENT.md#trusted-https-with-tailscale-serve).

## Using it

| Client | How to talk |
|---|---|
| Browser | Hold the on-screen button or the Space bar |
| Headless CLI (`cli/`) | Space toggles talk on/off in a terminal; runs under systemd |
| Radio gateway (`cli/`, `--aioc`) | Automatic: LAN speech keys the radio; received radio audio goes to the LAN |

The headless client and the gateway are documented in
[cli/README.md](cli/README.md); the AIOC hardware setup is in
[docs/AIOC.md](docs/AIOC.md).

## Configuration

Server environment variables (all optional):

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8443` | HTTPS / WebSocket port |
| `CERT_HOSTNAMES` | – | Extra DNS names for the self-signed certificate |
| `CERT_IPS` | – | Extra IPs for the self-signed certificate |
| `MAX_TRANSMIT_MS` | `60000` | Force-release the floor after this long without a release |
| `MAX_CLIENTS` | `32` | Maximum simultaneous connections |
| `PRIORITY_TOKEN` | unset | Shared secret (16+ chars) that grants priority/preemption; unset disables priority |
| `ALLOWED_ORIGINS` | unset | Comma-separated browser origins allowed to open the WebSocket |
| `ROGER_BEEP` | `off` | `on` makes every client (browsers, CLI, radio gateway) play a short beep when the channel is released. One global switch; restart the server to change it |

Generate a token with `openssl rand -hex 24`. Placeholder or short values are
refused: the server logs an error and runs with priority disabled.

## Security model

Read this before exposing it anywhere.

- **There is no authentication.** Anyone who can reach the port can join,
  listen and talk. The security boundary is network reachability: keep it on a
  trusted LAN or behind a VPN such as Tailscale, never on the public internet.
- **Audio is encrypted** (WebRTC DTLS-SRTP) between peers. Signaling runs over
  TLS.
- **Names are cosmetic.** Priority is granted by the token, not by name, and
  clients render the priority marker from a flag, not from the name text.
- **Floor control is cooperative.** The server enforces who *may* transmit,
  but a modified client can still open its own microphone track.
- **The CLI verifies TLS by default** and never sends the priority token over
  an unverified connection. Use `--ca-file` or `--pin-sha256` for a
  self-signed server.
- The server caps message size, message rate and connection count, and
  tolerates malformed input.

Found a problem? See [SECURITY.md](SECURITY.md).

## Documentation

- [docs/PROTOCOL.md](docs/PROTOCOL.md) – the signaling protocol, every message
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) – Docker, systemd, Tailscale Serve
- [docs/AIOC.md](docs/AIOC.md) – radio gateway: hardware, behaviour, safety
- [cli/README.md](cli/README.md) – headless client and gateway reference

## Project status

| Area | State |
|---|---|
| Server (floor control, priority, input hardening) | Automated tests (`npm test`) |
| Browser client | Used on desktop and phones; no automated tests |
| Headless CLI over WebRTC | Verified against the real server with synthetic audio |
| AIOC radio gateway | Run on real hardware: push-to-talk keying, radio audio to browsers and back, with Chrome and Firefox peers, reloads and reconnects. Unit-tested, plus Linux probes (`tools/`) for memory, object lifetime and per-peer audio negotiation. **No multi-day soak test yet** |
| Roaming between Wi-Fi access points | Reconnect logic present; weak spots on phones were reported and are not fully characterised |

Known limits: audio is half-duplex (one speaker at a time); a denied request is
dropped, not queued; STUN defaults to a public server (pass an empty STUN
setting on a LAN or VPN to avoid contacting it).

## Development

```bash
npm ci
npm test                      # server tests (node:test)
python3 -m unittest discover -s cli -p "test_*.py"   # CLI logic tests
```

The CLI tests stub GStreamer and pyserial, so they run on any OS.
CI runs both on every push. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
