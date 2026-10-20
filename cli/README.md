# LAN Walkie — headless CLI client and AIOC radio gateway

For machines with no browser: run this and join the same WebRTC mesh as the
browser clients (GStreamer's `webrtcbin` — real ICE/DTLS/SRTP, no bridge).
It has two modes:

- **Interactive** — a person at a terminal presses Space to talk.
- **Radio gateway** — an [AIOC](https://github.com/skuep/AIOC) USB cable ties
  a handheld radio to this machine. LAN audio goes out over the radio, and
  what the radio receives goes to the LAN. Unattended, systemd-friendly.

## Install (Ubuntu Server, no GUI needed)

```bash
sudo apt update
sudo apt install -y python3-gi gir1.2-gst-plugins-bad-1.0 \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-nice gstreamer1.0-tools \
    python3-websockets python3-serial alsa-utils
```

Everything is a stock Ubuntu package, so there is no `pip` step and nothing
unpinned to fetch.

## Connecting and TLS

`wss://` connections are **verified by default** against the system CAs.
That works out of the box if the server is behind Tailscale Serve (real
Let's Encrypt certificate, see the main README). For the server's own
self-signed certificate, pick one:

```bash
# Trust the server's cert file (copy certs/cert.pem from the server):
python3 lan_walkie_cli.py wss://<server>:8443 --ca-file ./walkie-cert.pem

# or pin its fingerprint (run on the server):
#   openssl x509 -in certs/cert.pem -noout -fingerprint -sha256
python3 lan_walkie_cli.py wss://<server>:8443 --pin-sha256 AB:CD:...

# last resort, no authentication of the server:
python3 lan_walkie_cli.py wss://<server>:8443 --insecure
```

With `--insecure` (or plain `ws://`) the client **never sends the priority
token** — a token must only go to a server you have authenticated. `--insecure`
prints the certificate fingerprint it saw so you can pin it afterwards.

## Interactive mode

```bash
python3 lan_walkie_cli.py wss://<server>:8443 --name "HeadlessNode" --ca-file ./walkie-cert.pem
```

Press **Space** to talk, **Space** again to stop, **q** to quit. (A terminal
can't see a key being released, so this is a toggle rather than hold-to-talk.)

Pick audio devices with `arecord -l` / `aplay -l`, then
`--source "alsasrc device=hw:1,0" --sink "alsasink device=hw:2,0"`, or
`--alsa-device plughw:CARD=...,DEV=0` to set both at once.

No audio hardware? Check just the network side:

```bash
python3 lan_walkie_cli.py wss://<server>:8443 --name Test \
    --source "audiotestsrc is-live=true wave=silence" --sink fakesink
```

## AIOC radio gateway

Hardware setup (flashing, cable, radio levels, udev) is in
[`docs/AIOC.md`](../docs/AIOC.md). Once the AIOC is
plugged in and the radio is connected:

```bash
arecord -l            # find the AIOC's card name
python3 lan_walkie_cli.py wss://<server>:8443 --name "Radio" --aioc \
    --alsa-device "plughw:CARD=<aioc-card-name>,DEV=0" --ca-file ./walkie-cert.pem
```

`--aioc` finds the AIOC's serial port by its USB ID (`1209:7388`);
`--ptt-serial /dev/ttyACM0` (or `/dev/aioc` with the udev rule) names it
explicitly.

### How it behaves

| Direction | Trigger | What happens |
|---|---|---|
| LAN → radio | Someone else holds the floor | The AIOC keys PTT (DTR=1, RTS=0) and their audio plays into its sound card. On release it stays keyed `--ptt-tail-ms` (300 ms), then unkeys. |
| radio → LAN | Radio audio above `--vox-threshold-db` for `--vox-attack-ms` | The gateway requests the floor like any user; if granted, the audio goes to everyone. Released after `--vox-hang-ms` (default 1500) of quiet. |

- **Permission comes first.** The radio is only keyed *after* the server
  announces someone else holds the floor, and the gateway only transmits to
  the LAN after being granted it. A denied request is dropped, not queued
  (same no-queue rule as every other client).
- **No echo loop.** VOX is ignored while the radio is keyed and for 0.5 s
  after it unkeys, so the gateway never hears its own transmission.
- **Safe states.** The radio is unkeyed on startup, on signaling loss, when
  the gateway itself takes the floor, on exit/crash handling, and after
  `--max-ptt-seconds` (90) regardless of what the server says.
- **Priority is intended for this node** (radio users can't see who is
  talking). It needs the token below, otherwise the gateway waits its turn
  like anyone else. Note a half-duplex handheld can't hear the radio side
  while the gateway is keyed, so priority helps when the gateway is idle or
  just after a LAN speaker releases, not mid-sentence.
- `--no-vox` makes it LAN → radio only (it never forwards received audio).

Not used: the AIOC's hardware COS input (HW 1.2, firmware 1.4+) and CM108
HID PTT. VOX plus serial PTT works on every firmware ≥ 1.2.0; firmware older
than 1.2.0 keyed on DTR alone and would key the radio as soon as the port
opened, so update it first.

### Tuning VOX

Watch `[radio: carrier/voice detected]` while the radio is idle. If it fires
on squelch noise, raise the squelch level on the radio first, then make
`--vox-threshold-db` less negative (e.g. `-35`). If the start of words is
clipped, lower `--vox-attack-ms`; if words are chopped mid-sentence, raise
`--vox-hang-ms`. Expect some clipping of the first syllable: the floor
request needs a round trip before the microphone is unmuted.

## Priority token

```bash
# server:   PRIORITY_TOKEN=$(openssl rand -hex 24)   (16+ chars, placeholders refused)
echo -n '<same token>' > /etc/lan-walkie-cli.token && chmod 600 /etc/lan-walkie-cli.token
python3 lan_walkie_cli.py ... --token-file /etc/lan-walkie-cli.token
```

`LAN_WALKIE_TOKEN` in the environment works too. `--token <value>` still
works but is visible to every user in `ps` and shell history, so it prints a
warning. The token is what the server checks, not the display name, and the
client only sends it over a verified connection.

## Running as a systemd service

```bash
sudo tee /etc/systemd/system/lan-walkie-cli.service << 'EOF'
[Unit]
Description=LAN Walkie AIOC radio gateway
After=network-online.target sound.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=your-username
SupplementaryGroups=audio dialout
EnvironmentFile=-/etc/lan-walkie-cli.env
ExecStart=/usr/bin/python3 /opt/lan-walkie-cli/lan_walkie_cli.py wss://<server>:8443 \
    --name "Radio" --aioc --alsa-device "plughw:CARD=<aioc-card-name>,DEV=0" \
    --ca-file /opt/lan-walkie-cli/walkie-cert.pem
Restart=always
RestartSec=5
NoNewPrivileges=true
CPUSchedulingPolicy=rr
CPUSchedulingPriority=80
IOSchedulingClass=realtime
IOSchedulingPriority=0
OOMScoreAdjust=-1000

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now lan-walkie-cli
```

Put `LAN_WALKIE_TOKEN=...` in `/etc/lan-walkie-cli.env` (mode 600, owned by
root) if the gateway needs priority.

- **`Restart=always`:** the client reconnects to the server by itself with
  backoff (1 s → 30 s); systemd only covers a crash, including one that
  exits 0, which `on-failure` would not restart.
- **Real-time scheduling** (`rr` 80, not `fifo` 99, so a spinning thread
  can't lock the machine) is a deliberate choice for an
  always-on box. It is OS scheduling, unrelated to walkie
  *floor* priority. Drop those five lines if the box also does other work
  you care about.
- Space-bar PTT doesn't exist under systemd; in gateway mode the radio is
  the only trigger.

## Command reference

| Flag | Meaning |
|---|---|
| `--name` | Display name (sanitised by the server, 24 chars) |
| `--source`, `--sink`, `--alsa-device` | Audio endpoints |
| `--stun ""` | Disable STUN (STUN sees this machine's public IP; unnecessary on LAN/Tailscale) |
| `--ca-file`, `--pin-sha256`, `--insecure` | TLS trust, see above |
| `--aioc`, `--ptt-serial`, `--ptt-tail-ms`, `--max-ptt-seconds` | Radio keying |
| `--no-vox`, `--vox-threshold-db`, `--vox-attack-ms`, `--vox-hang-ms` | Radio receive detection |
| `--token-file` | Priority token |
