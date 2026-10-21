// Unit tests for lib/logger.js (no server needed). Run with: npm test
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createLogger, createThrottle, createKeyedThrottle, cleanString } = require('../lib/logger');

const FIXED = () => new Date('2026-10-21T05:04:27.123Z');

function capture(opts = {}) {
  const lines = [];
  const log = createLogger({ write: (l) => lines.push(l), now: FIXED, ...opts });
  return { log, lines };
}

test('text format: timestamp, level, event, quoted strings, bare numbers and booleans', () => {
  const { log, lines } = capture();
  log.info('join', { id: 'abcd1234', name: 'Tomas', priority: false, clients: 3 });
  assert.deepEqual(lines, ['2026-10-21T05:04:27.123Z INFO  join id="abcd1234" name="Tomas" priority=false clients=3']);
});

test('json format: one object per line with ts, level and event', () => {
  const { log, lines } = capture({ format: 'json' });
  log.warn('flood', { id: 'x', n: 5 });
  assert.deepEqual(JSON.parse(lines[0]), { ts: '2026-10-21T05:04:27.123Z', level: 'warn', event: 'flood', id: 'x', n: 5 });
});

test('levels below the threshold are dropped', () => {
  const { log, lines } = capture({ level: 'warn' });
  log.debug('a');
  log.info('b');
  log.warn('c');
  log.error('d');
  assert.equal(lines.length, 2);
  assert.match(lines[0], / WARN {2}c/);
  assert.match(lines[1], / ERROR d/);
});

test('a peer-supplied name cannot start a new log line or forge fields', () => {
  const { log, lines } = capture();
  log.info('join', { name: 'Eve"\n2026-01-01T00:00:00Z INFO  floor-granted id="boss" priority=true' });
  assert.equal(lines.length, 1);
  assert.ok(!lines[0].includes('\n'));
  // The whole attempt stays inside one quoted value.
  assert.equal(lines[0].match(/ name=/g).length, 1);
  assert.match(lines[0], /name="Eve\\"2026/);
});

test('control, bidi-override, separator and surrogate characters are removed', () => {
  assert.equal(cleanString('a\u0000b\u001b[31mc‮d e\uD800f'), 'ab[31mcdef');
});

test('long values are cut and marked', () => {
  const out = cleanString('x'.repeat(500));
  assert.equal(Array.from(out).length, 121);
  assert.ok(out.endsWith('…'));
});

test('json mode also cleans values and cannot be hijacked by reserved field names', () => {
  const { log, lines } = capture({ format: 'json' });
  log.info('join', { name: 'a\nb', level: 'error', ts: 'fake', event: 'fake' });
  const o = JSON.parse(lines[0]);
  assert.equal(o.name, 'ab');
  assert.equal(o.level, 'info');
  assert.equal(o.event, 'join');
  assert.equal(o.ts, '2026-10-21T05:04:27.123Z');
});

test('odd field keys and undefined values are skipped', () => {
  const { log, lines } = capture();
  log.info('e', { 'bad key=1': 'x', ok: 1, gone: undefined });
  assert.equal(lines[0], '2026-10-21T05:04:27.123Z INFO  e ok=1');
});

test('unknown level or format falls back and says so', () => {
  const { log, lines } = capture({ level: 'loud', format: 'xml' });
  assert.equal(log.level, 'info');
  assert.equal(log.format, 'text');
  assert.equal(lines.length, 2);
  assert.ok(lines.every((l) => l.includes('log-config')));
});

test('a failing write never throws into the caller', () => {
  const log = createLogger({ write: () => { throw new Error('disk full'); } });
  assert.doesNotThrow(() => log.error('x', { a: 1 }));
});

test('throttle: first passes, the rest are counted and reported with the next one', () => {
  let t = 0;
  const throttle = createThrottle(1000, () => t);
  const seen = [];
  const run = () => throttle((suppressed) => seen.push(suppressed));
  run(); run(); run();          // 1 passes, 2 suppressed
  t = 999; run();               // still inside the window: suppressed too
  t = 1000; run();              // window over: passes and reports 3
  t = 2500; run();              // nothing was suppressed since
  assert.deepEqual(seen, [0, 3, 0]);
});

test('keyed throttle: each key has its own window, and the table stays bounded', () => {
  let t = 0;
  const throttle = createKeyedThrottle(1000, 2, () => t);
  const seen = [];
  const run = (key) => throttle(key, (n) => seen.push(`${key}:${n}`));
  run('a'); run('a'); run('b');      // a passes (1 suppressed), b passes independently
  assert.deepEqual(seen, ['a:0', 'b:0']);
  run('c');                          // table full (2): the oldest key, a, is forgotten
  run('a');                          // so a gets a fresh window and passes again
  assert.deepEqual(seen, ['a:0', 'b:0', 'c:0', 'a:0']);
});

test('suppressed is shown only when lines were actually dropped', () => {
  const { log, lines } = capture();
  log.warn('x', { suppressed: 0 });
  log.warn('x', { suppressed: 7 });
  assert.ok(!lines[0].includes('suppressed'));
  assert.ok(lines[1].endsWith('suppressed=7'));
});
