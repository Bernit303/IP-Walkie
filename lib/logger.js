// Small structured logger for server.js. No dependencies.
//
// One line per event, to stdout (Docker, Portainer and journald all capture it):
//   2026-10-21T05:04:27.123Z INFO  floor-granted id=3fa9c1d2 name="Tomas" priority=false
// or, with format "json", one JSON object per line.
//
// Everything that is not a constant here can come from a remote peer (names,
// user agents, origins, error text), so every string value is cleaned before it
// is written: control, format (e.g. right-to-left override), surrogate and
// line/paragraph-separator characters are removed and the length is capped. A
// peer therefore cannot add a fake log line or hide text with terminal tricks.
// Strings are always quoted in text mode, so a value can never run into the
// next field.

const LEVELS = { debug: 10, info: 20, warn: 30, error: 40 };
const MAX_VALUE_CHARS = 120;
const RESERVED = new Set(['ts', 'level', 'event']);

function cleanString(value, max = MAX_VALUE_CHARS) {
  const chars = Array.from(String(value));
  const cut = chars.length > max;
  const text = chars.slice(0, max).join('').replace(/[\p{C}\p{Zl}\p{Zp}]/gu, '');
  return cut ? `${text}…` : text;
}

function formatValue(value) {
  if (value === null) return 'null';
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : 'null';
  if (typeof value === 'boolean') return String(value);
  return JSON.stringify(cleanString(value));
}

function cleanFields(fields) {
  const out = {};
  for (const [key, value] of Object.entries(fields || {})) {
    if (value === undefined || (key === 'suppressed' && value === 0)) continue; // only say so when lines were actually dropped
    if (!/^[a-z][a-z0-9_]*$/.test(key) || RESERVED.has(key)) continue; // keys are ours; never let an odd one through
    out[key] = typeof value === 'string' ? cleanString(value) : value;
  }
  return out;
}

function createLogger({ level = 'info', format = 'text', write = (line) => process.stdout.write(line + '\n'), now = () => new Date() } = {}) {
  let threshold = LEVELS[level];
  let notes = [];
  if (threshold === undefined) {
    notes.push(`unknown LOG_LEVEL ${JSON.stringify(cleanString(level, 20))}, using info`);
    threshold = LEVELS.info;
  }
  const json = format === 'json';
  if (format !== 'text' && format !== 'json') {
    notes.push(`unknown LOG_FORMAT ${JSON.stringify(cleanString(format, 20))}, using text`);
  }

  function emit(lvl, event, fields) {
    if (LEVELS[lvl] < threshold) return;
    let line;
    try {
      const ts = now().toISOString();
      const clean = cleanFields(fields);
      if (json) {
        line = JSON.stringify({ ts, level: lvl, event, ...clean });
      } else {
        const rest = Object.entries(clean).map(([k, v]) => `${k}=${formatValue(v)}`).join(' ');
        line = `${ts} ${lvl.toUpperCase().padEnd(5)} ${event}${rest ? ' ' + rest : ''}`;
      }
      write(line);
    } catch {
      // Logging must never take the server down.
    }
  }

  const log = {
    debug: (event, fields) => emit('debug', event, fields),
    info: (event, fields) => emit('info', event, fields),
    warn: (event, fields) => emit('warn', event, fields),
    error: (event, fields) => emit('error', event, fields),
    level: Object.keys(LEVELS).find((k) => LEVELS[k] === threshold),
    format: json ? 'json' : 'text',
  };
  for (const note of notes) log.warn('log-config', { problem: note });
  return log;
}

// Rate limit for events a misbehaving client could repeat thousands of times.
// The first call in each window passes; the rest are counted and reported with
// the next one that passes (as `suppressed`). State lives in the returned
// closure, so a per-connection throttle is freed with the connection.
function createThrottle(intervalMs, now = () => Date.now()) {
  let last = -Infinity;
  let suppressed = 0;
  return (emit) => {
    const t = now();
    if (t - last < intervalMs) {
      suppressed++;
      return;
    }
    last = t;
    const count = suppressed;
    suppressed = 0;
    emit(count);
  };
}

// The same limit, kept separately per key (e.g. per IP address), so one noisy
// source cannot hide a different source's problem. The table is bounded: when it
// is full the oldest key is forgotten, which at worst lets one extra line through.
function createKeyedThrottle(intervalMs, maxKeys = 256, now = () => Date.now()) {
  const byKey = new Map();
  return (key, emit) => {
    const k = String(key);
    let throttle = byKey.get(k);
    if (!throttle) {
      if (byKey.size >= maxKeys) byKey.delete(byKey.keys().next().value);
      throttle = createThrottle(intervalMs, now);
      byKey.set(k, throttle);
    }
    throttle(emit);
  };
}

module.exports = { createLogger, createThrottle, createKeyedThrottle, cleanString };
