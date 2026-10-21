# Logging

`server.js` writes one line per event to standard output. Docker/Portainer
(container logs) and journald (`journalctl`) collect it, so there is no log file
to manage. The radio gateway (`cli/lan_walkie_cli.py`) logs through journald
(`journalctl -u lan-walkie-gateway`).

```
2026-10-21T05:18:23.905Z INFO  floor-preempted by_id="259bdaf4" by="Radio" from_id="14859156" from="Mom (phone)"
2026-10-21T05:18:24.012Z INFO  floor-released id="259bdaf4" name="Radio" reason="ptt-stop" held_ms=107
```

Time is UTC. `id` is the first 8 characters of the connection id, which lets you
follow one device from `connect` to `leave`.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `LOG_LEVEL` | `info` | `debug`, `info`, `warn` or `error`. Lines below the level are not written. |
| `LOG_FORMAT` | `text` | `text` (above) or `json` (one JSON object per line, for tools). |
| `LOG_STATS_MS` | `300000` | How often to write the `stats` summary line. `0` turns it off. |

An unknown value falls back to the default and says so in a `log-config` line.
Change them in the Portainer stack's environment variables and redeploy.

Docker keeps container logs without limit by default, so `docker-compose.yml`
caps them at 5 files of 10 MB.

## Events

### Normal operation (`info`)

| Event | Fields | When |
|---|---|---|
| `config` | version, node, pid, port, max_clients, max_transmit_ms, heartbeat_ms, priority (`enabled`/`disabled`), roger_beep (`on`/`off`), allowed_origins (count), log_level, log_format, stats_ms | Once at startup: the settings the server is actually running with. |
| `listening` | url | The server is accepting connections. |
| `cert-generate`, `cert-names` | covers | A new self-signed certificate is being created. |
| `connect` | id, ip, origin, ua, clients | A WebSocket opened. `ua` is the browser/device. Behind Tailscale Serve `ip` is the proxy's address. |
| `join` | id, name, priority, again, clients | The client sent its name. `again=true` means a second join on the same connection. |
| `floor-granted` | id, name, priority | Someone took the channel. |
| `floor-released` | id, name, reason, held_ms | The channel was freed. `reason` is `ptt-stop` (let go) or `disconnect` (connection lost while talking). |
| `floor-preempted` | by_id, by, from_id, from | A priority client took the channel from someone. Followed by `floor-granted`. |
| `floor-denied` | id, name, holder_id, holder | Someone pressed Talk while another person was talking. Limited to one per second per client. |
| `leave` | id, name, joined, reason, code, duration_s, clients | A connection ended. `reason` is `closed` (normal), `dead-connection`, `flood` or `socket-error`. |
| `tls-error` | ip, code | A browser refused the certificate, or something that is not TLS (a scanner, a plain `http://` request) hit the port. At most one per 5 s per address. |
| `stats` | clients, joined, floor, uptime_s, rss_mb | Periodic summary, so a quiet log still shows the server is alive. |
| `shutdown` | signal | Stopped by `SIGTERM`/`SIGINT` (e.g. `docker stop`). |

### Problems (`warn` / `error`)

| Event | Level | Fields | Meaning |
|---|---|---|---|
| `floor-timeout` | warn | id, name, held_ms, limit_ms | Someone held the channel for the whole `MAX_TRANSMIT_MS` without releasing (stuck button, frozen phone). |
| `dead-connection` | warn | id, name, check, silent_s | The server stopped hearing from a client and dropped it (phone left Wi-Fi, battery died). `check` is `app-ping` (browser) or `protocol-ping`. |
| `priority-denied` | warn | id, name, reason | A client offered a priority token that did not count. `reason` is `wrong-token`, or `priority-disabled-on-server` when the server has no valid `PRIORITY_TOKEN`. The token itself is never logged. |
| `priority-disabled` | error | reason, fix | `PRIORITY_TOKEN` is a placeholder or too short, so priority is off. |
| `invalid-frame` | warn | id, reason, bytes | A message that is not a JSON object. Only its size is logged. Limited to one per 5 s per client. |
| `flood` | warn | id, name, limit_per_s | A client sent more than 500 messages in a second and was dropped. |
| `reject-full` | warn | ip, max_clients | A connection was refused because the server is at `MAX_CLIENTS`. |
| `reject-origin` | warn | ip, origin | A browser was refused by `ALLOWED_ORIGINS`. |
| `socket-error` | warn | id, code, error | The connection broke (oversized frame, bad UTF-8, reset). |
| `message-error` | error | id, type, error | A bug while handling one message; that message was dropped and the server kept running. |
| `uncaught-exception`, `unhandled-rejection` | error | error, code, stack | The server is about to exit; the container restarts it. |
| `log-config` | warn | problem | A bad `LOG_LEVEL`/`LOG_FORMAT` was replaced by its default. |

### Detail (`debug` only)

`relay` / `relay-dropped` (who sent an offer, answer or ICE message to whom, never
its contents), `ptt-start-ignored`, `ptt-stop-ignored`, `ignored-before-join`,
`unknown-message`.

## Fields

When lines were dropped by a rate limit, the next line that does get written
carries `suppressed=N`. Limits are kept per source (per address, or per
connection), so one noisy device cannot hide another device's problem.

## What is never logged

- The priority token, from anyone, right or wrong.
- The contents of WebRTC messages (SDP, ICE candidates) or of any malformed frame.
- Audio. The server never receives it.

## Safe to read

Everything that comes from a client (names, device strings, origins, error text)
has control characters, text-direction overrides and line breaks removed, is cut
to 120 characters and is written in quotes. A client cannot invent log lines or
extra fields by choosing a clever name.

## Reading it

- **Portainer:** Containers, `lan-walkie`, Logs. Use the search box, for example
  `Mom`, `WARN`, `floor-timeout`.
- **Gateway machine:** `journalctl -u lan-walkie-gateway -f`.
- **Why did a phone drop out?** Search for its name: the `leave` line gives the
  `reason`. `dead-connection` means it went silent (Wi-Fi/sleep); `closed` means
  the app was closed or the page reloaded.
- **Certificate trouble on a phone?** Look for `tls-error` lines at that time.
- **Radio keeps talking over people?** `floor-granted` lines with `priority=true`
  and `floor-preempted` show who interrupted whom.
