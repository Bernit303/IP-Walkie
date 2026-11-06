# Changelog

## Unreleased

### Fixed
- Radio gateway going deaf: twice in one day (2026-11-05 evening and 2026-11-06) the gateway
  stayed connected but stopped receiving from the radio (`0 level readings`, `-inf dBFS` in the
  status line) while transmitting to the radio kept working, so replies from the radio never
  reached the phones. Both times it began within seconds of a phone connecting. The level meter
  now sits behind a queue that drops audio instead of blocking, so what happens to a peer's
  branch can no longer stop it, and a watchdog restarts the gateway (systemd brings it back
  in about 5 s) if the radio input or the send path produces nothing for 30 s. The restart
  report names the last peer add/remove and dumps the Python thread stacks.

- Server no longer crashes when a bad frame (invalid UTF-8, oversized) arrives on a connection
  that was just refused because the server is full.
- A client that joins while someone is talking now receives the current floor state. A gateway
  that reconnects mid-transmission starts relaying straight away instead of waiting for the
  next floor change.
- Gateway: the hard PTT limit now measures continuous keyed time. Back-to-back speakers
  (handovers inside the unkey tail) no longer restart the clock and keep the radio keyed.
- CLI: `WS://` (any letter case) is treated as plaintext, so the priority token is not sent.
- CLI: a floor grant that arrives after the talk request was released no longer opens the
  microphone.
- Gateway: roger-beep notes still scheduled when the connection resets, or when the next
  speaker starts, are cancelled instead of reopening the beep gate later.
- Browser: releasing Talk closes the microphone immediately instead of waiting for the server.

- Gateway: if the PTT watchdog's unkey write fails, it is retried every second instead of
  leaving a spent timer that stopped any new watchdog from being armed.
- Gateway: an error from the sound device itself (ALSA/Pulse/PipeWire) now unkeys and restarts
  the gateway; before, it was only printed and the gateway stayed connected but deaf or mute.
  Errors from a phone's branch are not fatal.
- Gateway: radio audio is not turned into a talk request before the server's welcome has
  arrived, so a request made before joining can no longer be lost and leave a false "talking".
- Gateway debug SDP logging strips control characters and is length-bounded.
- Server: a client that stops reading is disconnected once 512 KB is queued for it
  (`slow-consumer` log line) instead of growing server memory without limit.
- Browser: Join cannot run twice at once; an ended microphone track is replaced (also before
  a transmission starts) instead of being reused; a higher-ID phone that is left waiting for
  a new offer after a media failure rebuilds its session after 8 s; received audio and the
  roger beep are muted while the page is in the background.

### Added
- Optional `FRAME_ANCESTORS` (space- or comma-separated origins) lets the page be embedded in an
  iframe, e.g. a Home Assistant dashboard. Unset keeps framing denied. Invalid entries are
  ignored with a warning; `*` is never accepted.
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
