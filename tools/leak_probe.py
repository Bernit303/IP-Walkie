"""
Memory-growth probe for the gateway's incoming-audio path. No browser, server
or audio device needed: a fake remote peer inside this process sends Opus audio
to the gateway's own WebRTC code, and the resident memory is sampled while
different amounts of the receive chain are in place.

    python3 tools/leak_probe.py

Each variant runs in a fresh process. Use --variant app --seconds 120 for a
continuous-flow check, or add --upnp to reproduce the router-mapping issue.
The unlinked variant is a negative control and may report stalled audio.
"""

import os
import sys
import time
import argparse
import subprocess

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cli"))
import lan_walkie_cli as cli
Gst, GstSdp, GstWebRTC = cli.Gst, cli.GstSdp, cli.GstWebRTC


def rss_mb():
    for line in open("/proc/self/status"):
        if line.startswith("VmRSS"):
            return int(line.split()[1]) / 1024.0


def parse_sdp(text, kind):
    ok, m = GstSdp.SDPMessage.new()
    GstSdp.sdp_message_parse_buffer(text.encode(), m)
    return GstWebRTC.WebRTCSessionDescription.new(kind, m)


def run_variant(name, tail, seconds=30, upnp=False):
    remote = Gst.parse_launch(
        "webrtcbin name=rw bundle-policy=max-bundle "
        "audiotestsrc is-live=true wave=sine freq=440 ! audioconvert ! audioresample ! opusenc ! "
        "rtpopuspay pt=111 ! application/x-rtp,media=audio,encoding-name=OPUS,payload=111,clock-rate=48000 ! rw.")
    rw = remote.get_by_name("rw")
    cli.configure_upnp(rw, upnp)
    c = cli.WalkieClient("wss://x", "g", "audiotestsrc is-live=true wave=silence", "fakesink", None, "system")
    c.upnp = upnp
    c.build_pipeline()
    peer = cli.Peer("remote", "Remote")
    c.peers["remote"] = peer
    keep = []

    if tail != "app":
        def custom_pad_added(webrtcbin, pad, p):
            if pad.direction != Gst.PadDirection.SRC or keep:
                return
            keep.append(pad)
            if tail == "unlinked":
                return
            names = {"queue": ["queue"], "depay": ["queue", "rtpopusdepay"],
                     "decode": ["queue", "rtpopusdepay", "opusdec"]}[tail]
            els = []
            for n in names:
                e = Gst.ElementFactory.make(n)
                c.pipeline.add(e)
                e.sync_state_with_parent()
                els.append(e)
            sink = Gst.ElementFactory.make("fakesink")
            sink.set_property("sync", False)
            c.pipeline.add(sink)
            sink.sync_state_with_parent()
            els.append(sink)
            pad.link(els[0].get_static_pad("sink"))
            for a, b in zip(els, els[1:]):
                a.link(b)
        c.on_pad_added = custom_pad_added

    received = [0]
    original_pad_added = c.on_pad_added

    def count_buffer(pad, info):
        received[0] += 1
        return Gst.PadProbeReturn.OK

    def counted_pad_added(wb, pad, p):
        if pad.direction == Gst.PadDirection.SRC:
            pad.add_probe(Gst.PadProbeType.BUFFER, count_buffer)
        original_pad_added(wb, pad, p)

    c.on_pad_added = counted_pad_added

    pending = []

    def to_remote(msg):
        if msg["type"] == "answer":
            rw.emit("set-remote-description",
                    parse_sdp(msg["sdp"]["sdp"], GstWebRTC.WebRTCSDPType.ANSWER), Gst.Promise.new())
        elif msg["type"] == "ice":
            cand = msg["candidate"]
            rw.emit("add-ice-candidate", cand["sdpMLineIndex"], cand["candidate"])
    c._send_threadsafe = to_remote

    def on_remote_ice(wb, idx, cand):
        item = {"candidate": cand, "sdpMLineIndex": idx}
        if peer.webrtcbin is None:
            pending.append(item)
        else:
            c.handle_remote_ice("remote", item)
    rw.connect("on-ice-candidate", on_remote_ice)

    problems = []
    ice_state = {"v": "new"}

    def on_offer_created(promise, _u):
        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value("offer") if reply is not None else None
        if offer is None:
            problems.append("the fake remote peer could not create an offer: %s"
                            % (reply.to_string()[:200] if reply is not None else "no reply"))
            return
        rw.emit("set-local-description", offer, Gst.Promise.new())
        c.handle_offer("remote", offer.sdp.as_text())
        for item in pending:
            c.handle_remote_ice("remote", item)
        pending.clear()

    rw.connect("notify::ice-connection-state",
               lambda wb, _p: ice_state.__setitem__("v", wb.get_property("ice-connection-state").value_nick))
    remote.set_state(Gst.State.PLAYING)
    # Offer only after audio is flowing, so the sender already knows its media
    # type; creating it earlier yields an empty offer.
    time.sleep(1.5)
    rw.emit("create-offer", None, Gst.Promise.new_with_change_func(on_offer_created, None))
    time.sleep(7)

    got_audio = bool(keep) if tail != "app" else peer.mixer_pad is not None
    connected = ice_state["v"] in ("connected", "completed")
    valid = False
    if problems or not (connected and got_audio):
        print("%-30s INVALID: connection=%s, receiving audio=%s %s"
              % (name, ice_state["v"], got_audio, "; ".join(problems)), flush=True)
    else:
        a = rss_mb()
        before = received[0]
        previous = before
        stalled = False
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(min(5, max(0, deadline - time.monotonic())))
            current = received[0]
            stalled = stalled or current == previous
            print("sample rss=%.1f MiB received=%d delta=%d" %
                  (rss_mb(), current, current - previous), flush=True)
            previous = current
        b = rss_mb()
        valid = not stalled and ice_state["v"] in ("connected", "completed")
        print("%-30s %6.1f -> %6.1f MiB  (%+.1f MiB/min) packets=%d [%s]"
              % (name, a, b, (b - a) * 60.0 / seconds, received[0] - before,
                 "continuous receive flow" if valid else "INVALID: stalled receive flow"), flush=True)
    remote.set_state(Gst.State.NULL)
    c.pipeline.set_state(Gst.State.NULL)
    c.glib_loop.quit()
    time.sleep(1)
    return valid


VARIANTS = {"unlinked": "1 incoming pad left unlinked",
            "queue": "2 pad -> queue -> fakesink",
            "depay": "3 + rtpopusdepay", "decode": "4 + opusdec",
            "app": "5 the gateway's real chain"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=VARIANTS)
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--upnp", action="store_true", help="Re-enable UPnP on both peers for comparison")
    args = parser.parse_args()
    if not 5 <= args.seconds <= 300:
        parser.error("--seconds must be between 5 and 300")
    if args.variant:
        return 0 if run_variant(VARIANTS[args.variant], args.variant, args.seconds, args.upnp) else 1
    else:
        # Each variant starts with a fresh native heap and GLib context.
        failed = False
        for variant in VARIANTS:
            try:
                result = subprocess.run([sys.executable, __file__, "--variant", variant,
                                        "--seconds", str(args.seconds)] + (["--upnp"] if args.upnp else []),
                                        timeout=args.seconds + 60)
                failed = failed or result.returncode != 0
            except subprocess.TimeoutExpired:
                print("%s INVALID: timed out" % variant, flush=True)
                failed = True
        return int(failed)


if __name__ == "__main__":
    sys.exit(main())
