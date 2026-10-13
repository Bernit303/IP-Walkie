# Contributing

Thanks for looking. Small, focused changes are easiest to review.

## Before you start

- For anything larger than a bug fix, open an issue first so we can agree on
  the direction.
- The protocol is implemented three times (server, browser, CLI). A change to
  a message shape must update all three and `docs/PROTOCOL.md`.

## Running the tests

```bash
npm ci
npm test
python3 -m unittest discover -s cli -p "test_*.py"
```

Server tests start the real `server.js` on a free port and talk to it with real
WebSocket clients. CLI tests stub GStreamer and pyserial. If you change
behaviour, add or update a test.

## Guidelines

- Match the surrounding style; the browser client deliberately has no build
  step and no framework.
- Treat every peer-supplied value as hostile: validate type and size, and
  sanitise before printing or rendering.
- Anything that keys a radio must fail safe: an error path should end with the
  radio unkeyed.
- Don't commit certificates, keys, tokens, or real hostnames/addresses.

## Hardware changes

If you can test the AIOC gateway on real hardware, results (and fixes) are
especially welcome. See `docs/AIOC.md` for the list of unverified assumptions.
