# Changelog

## Unreleased

### Added
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
