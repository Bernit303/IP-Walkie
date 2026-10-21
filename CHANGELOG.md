# Changelog

## Unreleased

### Added
- Software squelch switch for the radio gateway: `--squelch on|off` (or `LAN_WALKIE_SQUELCH`
  in the service's env file). `on` is the gate exactly as before; `off` uses a much lower
  threshold (-55 dBFS, kept above the measured -64 dBFS idle floor), no attack delay and a
  300 ms pre-roll so the radio's own squelch does the filtering. Warns if the input never
  goes quiet.
- Structured server log (`docs/LOGGING.md`): timestamped one-line events for connections,
  joins, floor grants/denials/releases/timeouts/preemptions, dropped connections, bad
  frames, floods and certificate errors, plus a periodic summary. Never logs tokens or
  message contents; client-supplied text cannot forge log lines. `LOG_LEVEL`, `LOG_FORMAT`
  (`text`/`json`), `LOG_STATS_MS`. Container logs are now capped (5 x 10 MB).
- Roger beep: a short tone when the channel is released, on browsers, the CLI and
  (over the air) the radio gateway. One global switch, `ROGER_BEEP=on` on the server
  (default off), announced to clients in `welcome`.
- AIOC radio gateway mode in the CLI (`--aioc`, `--ptt-serial`): serial
  DTR/RTS PTT, VOX for received audio, floor-gated keying with local watchdog.
- Server limits: message size, message rate and connection cap; optional
  `ALLOWED_ORIGINS`.
- Automated server and CLI tests, CI, MIT license, security policy, docs.

### Changed
- CLI verifies TLS by default (`--ca-file`, `--pin-sha256`, `--insecure`) and
  only sends the priority token on a verified connection; token is read from
  `--token-file` / `LAN_WALKIE_TOKEN`.
- Server rejects weak/placeholder priority tokens, sanitises names and
  ignores `ptt-start` from the current floor holder.
- Container runs as a non-root user with a read-only filesystem.

### Fixed
- Gateway never sent audio to a browser that started the call (its answer came back
  `a=recvonly`): the sender was labelled Opus payload 96 for everyone, while Chrome offers
  111. Audio is now encoded once and packetized per peer with that peer's payload number;
  a second offer from the same browser (it rebuilt its connection) gets a fresh connection.
- Gateway sent no offer when it had to start the call, because it attached its
  negotiation listener too late; it also sent an empty first offer.
- Radio stayed keyed while peers were torn down on signaling loss; it is now unkeyed first.
- "Radio is talking" flickered: the gateway's own floor grant triggered the post-transmit
  VOX lockout.
- VOX never saw radio audio: an idle audio mixer stalled the whole pipeline.
- Native object-ownership errors when removing peers (ICE wrapper reference count).
- Gateway grew by about 230 MB/min while a WebRTC peer was connected (OOM-killed within
  an hour). Cause: libnice's UPnP port-mapping path. UPnP is now off by default
  (`--upnp` re-enables it); see `docs/MEMORY_LEAK_AUDIT.md`. Peer teardown also releases
  its signal handlers and WebRTC sink pad.
- Server crashes on `null` / non-object frames, non-string names and invalid
  UTF-8 frames.
- Browser could hold the floor open when the button was released before the
  grant arrived; CLI dropped its talk intent on a stale floor broadcast.

### Removed
- Raspberry Pi Pico firmware and the `TX:`/`RX:` serial protocol (replaced by
  the AIOC).

## Earlier

Browser mesh, floor control and priority, headless CLI with auto-reconnect,
Docker deployment.
