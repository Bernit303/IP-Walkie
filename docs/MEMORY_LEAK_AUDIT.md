# Native-memory audit — 20 October 2026

Audited repository revision: `ea78d62e340cacfd962d209930c8ce0b60ef0574`.
Tests ran over SSH on the affected Ubuntu machine, with synthetic audio and no
radio/PTT access. Production files were not modified.

## Finding and fix

**High confidence: the reproduced rapid growth comes from the UPnP port-mapping
path activated by libnice, involving libgupnp-igd, libgupnp and libsoup.** Turning
off `NiceAgent.upnp` on both synthetic endpoints stops the growth while ICE and
incoming RTP continue working. Disabling STUN does not disable UPnP.

The heaptrack profile contains 18.78 MB of peak retained allocations through
`g_string_insert_len -> libgupnp -> gupnp_service_proxy_call_action_async ->
libgupnp-igd`, and another 5.63 MB through `soup_message_new` in the same path.
These are examples within a much larger GLib allocation group. They are not
evidence of an Opus decoder or RTP payload-buffer leak.

The exact router response and individual missing-reference bug have not been
proven. A retry loop is the leading explanation: upstream 1.6.0 contains
`gupnp_simple_igd_call_add_port_mapping` and a conflict retry path in
`_service_proxy_added_port_mapping`. The installed distro build may contain
patches, and stripped stack frames do not identify the exact call site.

The application fix configures the underlying NiceAgent before candidate
gathering, with UPnP disabled by default and an explicit `--upnp` opt-in:

```python
# Keep this wrapper alive with the webrtcbin. A temporary chained wrapper
# caused an ICE-object lifetime crash in the affected 1.24.2 environment.
webrtcbin._walkie_ice = webrtcbin.get_property("ice-agent")
agent = webrtcbin._walkie_ice.get_property("agent")
agent.set_property("upnp", False)
```

This retains Python, GStreamer, RTP/Opus, DTLS/SRTP and the existing signaling.
LAN/Tailscale host candidates do not need router port mappings. Networks relying
on UPnP mappings can lose that connectivity route; STUN remains independently
configured. Chrome interoperability follows the same protocol but has not been
retested end-to-end in this audit.

## Direct measurements

| Experiment | Result |
| --- | --- |
| Original code, heaptrack, 30-second sample | 99.5 → 207.2 MiB; +215.3 MiB/min |
| Original code plus UPnP-off on both peers, 30-second sample | 50.5 → 50.5 MiB; rounded +0.0 MiB/min |
| UPnP-off live allocation counter | About 8.9 MB allocated; essentially flat |
| Patched code, 120-second sample | 49.7 → 49.8 MiB; +0.1 MiB/min; 6,000 incoming RTP buffers, no stalled interval |
| Baseline live allocation counter | 49,990,320 bytes at 10 s → 140,745,520 at 35 s |

The baseline probe hung during teardown and was terminated by the outer timeout.
Consequently heaptrack's exit-time "leaked" totals include allocations still
reachable at forced termination. The growing live heap, allocation stacks, and
UPnP A/B test support the finding; shutdown leak totals alone do not prove it.
One initial UPnP experiment crashed when an ICE wrapper was temporary; retaining
the wrapper fixed that failure in subsequent runs.

The patched probe adds per-five-second receive-buffer counts, single-variant
selection, fresh processes between variants, and an UPnP-on comparison switch.
The original unlinked variant remains intentionally capable of stalling; the
new output explicitly marks a stalled receive stream invalid.

## Interpreting the earlier observations

* No peer means no peer ICE gathering and hence no peer UPnP mapping requests.
* Muting audio does not stop ICE-side router operations. Equal idle/active
  slopes are therefore consistent with this finding.
* Removing decode and mixer stages leaves the UPnP path unchanged.
* The old probe measured both endpoints together. Disabling UPnP on only the
  gateway can leave the synthetic remote leaking and produce a false failure.
* A receive pad appearing is not proof of sustained audio. The old probe's
  "audio flowing" label did not check buffers after connection.
* `threading.active_count()` measures Python-managed threads, not all native
  threads. The baseline had about 33 native threads, despite the earlier count
  of two Python threads. `sys.getallocatedblocks()` likewise excludes most
  native library allocations.
* Empty application queues do not exclude native async requests or internal
  queues. RSS remaining high after teardown is not by itself a leak proof.
* At 230 MB/min, 35–40 minutes would add about 8–9 GB. The quoted 3.48 GB
  anonymous RSS at OOM does not fit that rate continuously without additional
  facts such as swapping, a changed rate, or differing connected durations.
* Dividing growth by audio packet rate was arithmetic, not attribution. The
  profile gives a different allocation source. In libnice 0.1.21 the nicesrc
  callback allocates the received length; a nearby 65536 constant is not proof
  that every audio packet leaks 64 KiB.

## Reproduce and falsify

From the patched repository on Linux (the isolated test copy is
`/tmp/ip-walkie-audit`):

```bash
timeout -k 5s 150s python3 tools/leak_probe.py --variant app --seconds 120
timeout -k 5s 60s python3 tools/leak_probe.py --variant app --seconds 30 --upnp
```

The UPnP-off run must retain completed/connected ICE and show increasing packet
counts in every interval. Sustained baseline-size growth with UPnP confirmed
off would falsify the workaround for that run. A consistently flat UPnP-on run
would mean the router-triggered condition is intermittent or has changed.

For allocation profiling (heaptrack was installed during this audit):

```bash
timeout -k 5s 65s heaptrack -o /tmp/walkie-upnp \
  python3 tools/leak_probe.py --variant app --seconds 30 --upnp
heaptrack_print -f /tmp/walkie-upnp.zst -a 0 -T 0 -p 1 -l 0 -n 3 -s 2
```

Use the actual filename printed by heaptrack if its compression suffix differs.
Compare retained/peak bytes and stacks, not allocation-call counts alone.

For the original checkout without patching it:

```bash
timeout -k 5s 65s python3 /tmp/ip-walkie-native-memory-probe.py \
  --repo ~/IP-Walkie --seconds 30
timeout -k 5s 65s python3 /tmp/ip-walkie-native-memory-probe.py \
  --repo ~/IP-Walkie --seconds 30 --disable-upnp
```

Optional allocator-control experiment, only if native allocations no longer
explain RSS: repeat that baseline with `MALLOC_ARENA_MAX=2` or `--trim` separately.
A large RSS reduction after trim with flat live allocations supports retained
free pages; continued live-allocation growth rejects allocator tuning as the
root fix. Those experiments were unnecessary after the UPnP comparison.

GStreamer's leaks tracer can supplement heaptrack:

```bash
GST_TRACERS=leaks GST_DEBUG=GST_TRACER:7 GST_LEAKS_TRACER_SIG=1 \
  python3 tools/leak_probe.py --variant app --seconds 120
```

Send `kill -USR1 PID` to that explicitly identified probe PID at two times to
log living objects. Growing GstBuffer/GstMessage counts implicate those objects;
flat counts do not exclude libgupnp/libsoup allocations. Do not expect complete
shutdown reporting without `Gst.deinit()` and released Python references.

## Confidence and remaining validation

Subjective ranking for the rapid growth reproduced here: UPnP path 98%, another
native transport defect 1%, allocator/application effects 1%. These are judgment
estimates, not statistical measurements. Confidence in a specific router error
or exact library source-line defect is lower and is deliberately not quantified
as if measured.

The best remaining experiment is the patched real gateway with one Chrome peer
for at least ten minutes, including both PTT directions and reconnects. That
validates the production topology and radio behavior beyond the synthetic test.

Preferred remedy order: disable UPnP as implemented; if UPnP is required, test a
coherent updated libnice/GUPnP/libsoup stack against the same reproducer. No
particular package release is claimed to fix this unverified lower-level bug.
Allocator tuning, decoder changes, periodic restarts, and replacing GStreamer
are not justified by this evidence.

The patch also disconnects peer signal handlers, releases the requested WebRTC
sink pad, and clears peer-owned references on removal. This addresses a separate
cleanup ownership issue and is not presented as the cause of steady-state growth.

## Sources

* [NiceAgent UPnP property, enabled by default](https://libnice.freedesktop.org/libnice/NiceAgent.html#NiceAgent--upnp)
* [GUPnP IGD 1.6.0 source](https://github.com/GNOME/gupnp-igd/blob/1.6.0/libgupnp-igd/gupnp-simple-igd.c)
* [libnice 0.1.21 nicesrc source](https://github.com/libnice/libnice/blob/0.1.21/gst/gstnicesrc.c)
* [Heaptrack](https://github.com/KDE/heaptrack)
* [GStreamer leaks tracer](https://gstreamer.freedesktop.org/documentation/coretracers/leaks.html)

Server versions: GStreamer 1.24.2; libnice 0.1.21-2build3; python3-gi 3.48.2-1;
glibc 2.39-0ubuntu8.9; GLib 2.80.0-6ubuntu3.9; libgupnp-igd 1.6.0-3build3;
libgupnp 1.6.6-1build3; libsoup 3.4.4-5ubuntu0.9.

Local validation: 35 Python unit tests passed, Python compilation passed, and
`git diff --check` passed. The 120-second native test exited normally.

## Follow-up: ICE object ownership (measured 21 October 2026)

An independent review of `7955d9c` found GLib criticals
(`g_object_get_qdata: assertion 'G_IS_OBJECT (object)' failed`) when peers are
removed. `tools/ice_lifetime_probe.py` creates and destroys 20 `webrtcbin`
elements per variant, each in a fresh process, on the affected machine
(GStreamer 1.24.2, python3-gi 3.48.2):

| Variant | GLib criticals | Result |
| --- | --- | --- |
| no UPnP change (control) | 0 | clean |
| wrapper stored on the element (as pushed in `7955d9c`) | 3 | `ice refcount after get=1` |
| wrapper released after the element is gone | 5 | |
| wrapper kept forever / plus `ice.ref()` | 2 / 5 | **segfault** |
| **one extra `g_object_ref`, nothing stored** | **0** | `refcount 1 -> 2`, UPnP readback off, fds/RSS same as control |
| extra ref + stored on element / released after | 0 / 0 | clean |

Cause: `webrtcbin` creates its ICE object without sinking the floating
reference, so the PyGObject wrapper takes over that single reference. Whichever
side releases first leaves the other holding a freed object. The fix gives the
wrapper its own reference (`configure_upnp`), only when the refcount reads 1, so
a GStreamer release that fixes this itself is left untouched. If the reference
cannot be added, or UPnP cannot be confirmed off, the gateway refuses to connect
(`--upnp` accepts the risk).
