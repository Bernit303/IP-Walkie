"""
Logic tests for the CLI. GStreamer, termios and pyserial are stubbed, so these
run on any OS without audio hardware:

    python3 -m unittest discover -s cli -p "test_*.py"
"""

import asyncio
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock


class FakeSerial:
    instances = []

    def __init__(self):
        self.port = None
        self.dtr = None
        self.rts = None
        self.is_open = False
        FakeSerial.instances.append(self)

    def open(self):
        self.is_open = True

    def close(self):
        self.is_open = False


def load_cli():
    for name in ("gi", "gi.repository", "termios", "tty"):
        sys.modules[name] = MagicMock()
    serial_mod = types.ModuleType("serial")
    serial_mod.Serial = FakeSerial
    sys.modules["serial"] = serial_mod
    path = Path(__file__).with_name("lan_walkie_cli.py")
    spec = importlib.util.spec_from_file_location("lan_walkie_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = load_cli()


class VoxTests(unittest.TestCase):
    def test_start_after_attack_and_stop_after_hang(self):
        v = cli.Vox(threshold_db=-40, attack_ms=60, hang_ms=800)
        self.assertEqual([v.update(-20, 20) for _ in range(3)], [None, None, "start"])
        self.assertTrue(all(v.update(-60, 20) is None for _ in range(39)))
        self.assertEqual(v.update(-60, 20), "stop")

    def test_blocked_resets_and_stops(self):
        v = cli.Vox()
        for _ in range(5):
            v.update(-10, 20)
        self.assertEqual(v.update(-10, 20, blocked=True), "stop")
        self.assertFalse(v.active)

    def test_attack_needs_continuous_audio(self):
        v = cli.Vox()
        v.update(-10, 20)
        v.update(-90, 20)
        self.assertEqual(v._above_ms, 0)


class LevelParsingTests(unittest.TestCase):
    def test_uses_rms_list(self):
        s = MagicMock()
        s.get_value.return_value = [-31.5, -28.0]
        self.assertEqual(cli.WalkieClient._level_from_structure(s), -28.0)

    def test_falls_back_to_text_form(self):
        s = MagicMock()
        s.get_value.side_effect = TypeError("GValueArray")
        s.to_string.return_value = "level, endtime=(guint64)1, rms=(double){ -41.2, -39.9 }, peak=(double){ -30 };"
        self.assertEqual(cli.WalkieClient._level_from_structure(s), -39.9)

    def test_fallback_handles_angle_bracket_arrays(self):
        s = MagicMock()
        s.get_value.side_effect = TypeError("GValueArray")
        s.to_string.return_value = "level, endtime=(guint64)1, rms=(double)< -41.2, -35.5 >, peak=(double)< -30 >;"
        self.assertEqual(cli.WalkieClient._level_from_structure(s), -35.5)

    def test_real_ubuntu_24_04_message(self):
        s = MagicMock()
        s.get_value.side_effect = TypeError("GValueArray")
        s.to_string.return_value = (
            "level, endtime=(guint64)200000000, timestamp=(guint64)0, "
            "rms=(GValueArray)< -48.439406690821514, -48.439406690821514 >, "
            "peak=(GValueArray)< -46.28105621278533, -46.28105621278533 >;"
        )
        self.assertAlmostEqual(cli.WalkieClient._level_from_structure(s), -48.4394, places=3)

    def test_unparseable_returns_none(self):
        s = MagicMock()
        s.get_value.return_value = None
        s.to_string.return_value = "level, nothing=(int)1;"
        self.assertIsNone(cli.WalkieClient._level_from_structure(s))


class PipelineTests(unittest.TestCase):
    def test_mixer_has_permanent_silent_input(self):
        # Regression: an idle audiomixer stalls the whole pipeline, which
        # starved the capture-side level meter that VOX depends on.
        cli.Gst.parse_launch.reset_mock()
        c = cli.WalkieClient("wss://x", "R", "alsasrc", "alsasink", None, "system", vox=cli.Vox())
        c.build_pipeline()
        launch = cli.Gst.parse_launch.call_args[0][0]
        self.assertIn("audiotestsrc is-live=true wave=silence ! mix.", launch)
        self.assertIn("level name=lvl", launch)


CHROME_OFFER = (
    "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 111 63 9 0 8 13 110 126\r\nc=IN IP4 0.0.0.0\r\n"
    "a=mid:0\r\na=sendrecv\r\na=rtpmap:111 opus/48000/2\r\na=rtpmap:63 red/48000/2\r\n"
    "a=rtpmap:9 G722/8000\r\na=rtpmap:0 PCMU/8000\r\na=rtpmap:8 PCMA/8000\r\n"
    "a=rtpmap:13 CN/8000\r\na=rtpmap:110 telephone-event/48000\r\n"
)


class OfferedPayloadTests(unittest.TestCase):
    def test_chrome_offer_uses_payload_111(self):
        self.assertEqual(cli.offered_opus_payload(CHROME_OFFER), 111)

    def test_gateway_style_offer_uses_payload_96(self):
        sdp = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 96\r\na=sendrecv\r\na=rtpmap:96 OPUS/48000/2\r\n"
        self.assertEqual(cli.offered_opus_payload(sdp), 96)

    def test_other_dynamic_payload_numbers_are_followed(self):
        sdp = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 109\r\na=rtpmap:109 opus/48000/2\r\n"
        self.assertEqual(cli.offered_opus_payload(sdp), 109)

    def test_video_section_is_not_mistaken_for_audio(self):
        sdp = (
            "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 100\r\na=rtpmap:100 VP8/90000\r\n"
            "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=rtpmap:111 opus/48000/2\r\n"
        )
        self.assertEqual(cli.offered_opus_payload(sdp), 111)

    def test_rejected_audio_section_is_skipped(self):
        sdp = (
            "v=0\r\nm=audio 0 UDP/TLS/RTP/SAVPF 111\r\na=rtpmap:111 opus/48000/2\r\n"
            "m=audio 9 UDP/TLS/RTP/SAVPF 109\r\na=rtpmap:109 opus/48000/2\r\n"
        )
        self.assertEqual(cli.offered_opus_payload(sdp), 109)

    def test_opus_mapping_not_listed_on_the_m_line_is_ignored(self):
        sdp = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 0\r\na=rtpmap:0 PCMU/8000\r\na=rtpmap:111 opus/48000/2\r\n"
        with self.assertRaises(ValueError):
            cli.offered_opus_payload(sdp)

    def test_offer_without_opus_is_refused(self):
        with self.assertRaises(ValueError):
            cli.offered_opus_payload("v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 0\r\na=rtpmap:0 PCMU/8000\r\n")


class PerPeerPayloadTests(unittest.IsolatedAsyncioTestCase):
    """The shared encoder feeds one RTP packetizer PER PEER, numbered as that peer's offer says."""

    def make_client(self):
        # the real GstSdp.SDPMessage.new() returns (ok, message)
        cli.GstSdp.SDPMessage.new.return_value = (True, MagicMock())
        c = cli.WalkieClient("wss://x", "R", "src", "sink", None, "system")
        c.pipeline, c.tee = MagicMock(), MagicMock()
        c.sent = []
        c._send_threadsafe = c.sent.append
        return c

    def build(self, c, peer, initiator):
        made = []

        def make(factory, name=None):
            el = MagicMock(name=factory)
            el.factory = factory
            el.get_property.return_value.get_property.return_value.get_property.return_value = False
            made.append(el)
            return el

        cli.Gst.ElementFactory.make.side_effect = make
        try:
            c.create_peer_connection(peer, initiator)
        finally:
            cli.Gst.ElementFactory.make.side_effect = None
        return made

    def test_payloader_and_caps_follow_the_peers_payload_number(self):
        for number in (96, 111, 109):
            c = self.make_client()
            peer = cli.Peer("p", "P")
            peer.opus_payload = number
            made = self.build(c, peer, initiator=False)
            pay = next(e for e in made if e.factory == "rtpopuspay")
            pay.set_property.assert_any_call("pt", number)
            caps = next(e for e in made if e.factory == "capsfilter")
            self.assertTrue(caps.set_property.called)
            cli.Gst.Caps.from_string.assert_called_with(
                "application/x-rtp,media=audio,encoding-name=OPUS,"
                f"payload={number},clock-rate=48000,encoding-params=(string)2"
            )

    def test_each_peer_gets_its_own_payloader(self):
        c = self.make_client()
        first, second = cli.Peer("a", "A"), cli.Peer("b", "B")
        first.opus_payload, second.opus_payload = 111, 96
        self.build(c, first, initiator=False)
        self.build(c, second, initiator=False)
        self.assertIsNot(first.send_payloader, second.send_payloader)

    def test_the_shared_encoder_output_is_not_packetized_before_the_tee(self):
        cli.Gst.parse_launch.reset_mock()
        c = cli.WalkieClient("wss://x", "R", "alsasrc", "alsasink", None, "system")
        c.build_pipeline()
        launch = cli.Gst.parse_launch.call_args[0][0]
        self.assertIn("opusenc bitrate=32000 ! tee name=t", launch)
        self.assertNotIn("rtpopuspay", launch)

    async def test_a_chrome_offer_builds_the_sender_with_payload_111(self):
        c = self.make_client()
        c.peers["chrome"] = cli.Peer("chrome", "Chrome")
        seen = []

        def fake_create(peer, initiator):
            seen.append((peer.opus_payload, initiator))
            return MagicMock()

        c.create_peer_connection = fake_create
        c.handle_offer("chrome", CHROME_OFFER)
        self.assertEqual(seen, [(111, False)])

    async def test_a_second_offer_from_the_same_browser_starts_a_fresh_connection(self):
        c = self.make_client()
        c.peers["chrome"] = cli.Peer("chrome", "Chrome")
        created = []
        removed = []

        def fake_create(peer, initiator):
            created.append(peer)
            return MagicMock()

        c.create_peer_connection = fake_create
        c.remove_peer = lambda pid: (removed.append(pid), c.peers.pop(pid))
        c.handle_offer("chrome", CHROME_OFFER)
        self.assertEqual(removed, [])
        c.handle_offer("chrome", CHROME_OFFER)  # the browser rebuilt its connection
        self.assertEqual(removed, ["chrome"])
        self.assertEqual(len(created), 2)
        self.assertIsNot(created[0], created[1])

    async def test_a_changed_payload_number_also_starts_a_fresh_connection(self):
        c = self.make_client()
        peer = cli.Peer("x", "X")
        peer.webrtcbin = MagicMock()
        peer.opus_payload = 96
        c.peers["x"] = peer
        c.create_peer_connection = lambda p, initiator: MagicMock()
        c.remove_peer = lambda pid: c.peers.pop(pid)
        c.handle_offer("x", CHROME_OFFER)
        self.assertEqual(c.peers["x"].opus_payload, 111)

    def test_only_one_offer_is_sent_and_never_before_the_sender_exists(self):
        c = self.make_client()
        peer = cli.Peer("p", "P")
        c.peers["p"] = peer
        wb = MagicMock()
        c.on_negotiation_needed(wb, "p")  # sender not ready yet: no empty offer
        wb.emit.assert_not_called()
        peer.send_ready = True
        c.on_negotiation_needed(wb, "p")
        c.on_negotiation_needed(wb, "p")  # a second notification is coalesced
        self.assertEqual(wb.emit.call_count, 1)

    def test_ice_that_arrives_before_the_offer_is_kept_and_replayed(self):
        c = self.make_client()
        peer = cli.Peer("p", "P")
        c.peers["p"] = peer
        c.handle_remote_ice("p", {"candidate": "candidate:1 1 udp 1 10.0.0.2 5000 typ host", "sdpMLineIndex": 0})
        self.assertEqual(len(peer.pending_ice), 1)
        for i in range(400):  # a flood cannot grow the buffer without bound
            c.handle_remote_ice("p", {"candidate": f"candidate:{i}", "sdpMLineIndex": 0})
        self.assertLessEqual(len(peer.pending_ice), 256)

    async def test_only_the_side_that_must_offer_creates_a_connection_up_front(self):
        c = self.make_client()
        c.my_id = "bbb"
        created = []
        c.create_peer_connection = lambda peer, initiator: created.append((peer.id, initiator)) or MagicMock()
        await c.handle_message(json.dumps({"type": "peers", "peers": [
            {"id": "aaa", "name": "Lower"},   # "bbb" > "aaa": the browser offers, we answer
            {"id": "ccc", "name": "Higher"},  # "bbb" < "ccc": we offer
        ]}))
        self.assertEqual(created, [("ccc", True)])
        self.assertIn("aaa", c.peers)  # known, but its connection waits for its offer


class PeerWiringTests(unittest.TestCase):
    def test_upnp_is_disabled_before_pipeline_add(self):
        c = cli.WalkieClient("wss://x", "R", "src", "sink", None, "system")
        c.pipeline, c.tee = MagicMock(), MagicMock()
        wb = MagicMock()
        queue = MagicMock()
        ice = wb.get_property.return_value
        agent = ice.get_property.return_value
        agent.get_property.return_value = False  # readback: UPnP is off

        def added(el):
            if el is wb:
                agent.set_property.assert_called_once_with("upnp", False)

        c.pipeline.add.side_effect = added
        cli.Gst.ElementFactory.make.side_effect = [wb, queue, MagicMock(), MagicMock()]
        try:
            c.create_peer_connection(cli.Peer("p", "P"), False)
        finally:
            cli.Gst.ElementFactory.make.side_effect = None

    # --- ICE object ownership (measured on GStreamer 1.24.2) ---

    class _FakeAgent:
        def __init__(self):
            self.props = {"upnp": True}

        def set_property(self, name, value):
            self.props[name] = value

        def get_property(self, name):
            return self.props[name]

    class _FakeIce:
        def __init__(self, refcount):
            self.__grefcount__ = refcount
            self.agent = PeerWiringTests._FakeAgent()

        def get_property(self, name):
            return self.agent

    class _FakeWebrtcbin:
        def __init__(self, refcount):
            self.ice = PeerWiringTests._FakeIce(refcount)

        def get_property(self, name):
            return self.ice

    def test_wrapper_gets_its_own_reference_when_refcount_is_one(self):
        wb = self._FakeWebrtcbin(1)
        added = []
        original = cli._add_gobject_ref
        cli._add_gobject_ref = added.append
        try:
            cli.configure_upnp(wb, False)
        finally:
            cli._add_gobject_ref = original
        self.assertEqual(added, [wb.ice])
        self.assertFalse(wb.ice.agent.props["upnp"])
        self.assertFalse(hasattr(wb, "_walkie_ice"), "the wrapper must not be parked on the element")

    def test_no_extra_reference_when_the_wrapper_already_owns_one(self):
        wb = self._FakeWebrtcbin(2)  # a GStreamer that sinks the reference itself
        added = []
        original = cli._add_gobject_ref
        cli._add_gobject_ref = added.append
        try:
            cli.configure_upnp(wb, False)
        finally:
            cli._add_gobject_ref = original
        self.assertEqual(added, [])

    def test_failure_to_add_the_reference_refuses_to_connect(self):
        wb = self._FakeWebrtcbin(1)
        original = cli._add_gobject_ref

        def boom(obj):
            raise OSError("libgobject not found")

        cli._add_gobject_ref = boom
        try:
            with self.assertRaises(cli.UpnpConfigError):
                cli.configure_upnp(wb, False)
        finally:
            cli._add_gobject_ref = original

    def test_upnp_setup_failure_refuses_to_connect(self):
        # A failed "turn UPnP off" must not look like success: continuing would
        # bring back the ~230 MB/min growth silently.
        wb = MagicMock()
        wb.get_property.side_effect = TypeError("no such property")
        with self.assertRaises(cli.UpnpConfigError):
            cli.configure_upnp(wb, False)

    def test_upnp_still_on_after_setting_it_off_refuses(self):
        wb = MagicMock()
        agent = wb.get_property.return_value.get_property.return_value
        agent.get_property.return_value = True  # readback says it is still on
        with self.assertRaises(cli.UpnpConfigError):
            cli.configure_upnp(wb, False)

    def test_upnp_off_is_confirmed_by_readback(self):
        wb = MagicMock()
        agent = wb.get_property.return_value.get_property.return_value
        agent.get_property.return_value = False
        cli.configure_upnp(wb, False)  # must not raise
        agent.set_property.assert_called_once_with("upnp", False)

    def test_opting_in_with_upnp_flag_never_raises(self):
        wb = MagicMock()
        wb.get_property.side_effect = TypeError("no such property")
        cli.configure_upnp(wb, True)  # operator accepted the default behaviour

    def test_refused_upnp_adds_nothing_to_the_pipeline(self):
        c = cli.WalkieClient("wss://x", "R", "src", "sink", None, "system")
        c.pipeline, c.tee = MagicMock(), MagicMock()
        wb = MagicMock()
        wb.get_property.side_effect = TypeError("no such property")
        cli.Gst.ElementFactory.make.side_effect = [wb]
        try:
            with self.assertRaises(cli.UpnpConfigError):
                c.create_peer_connection(cli.Peer("p", "P"), False)
        finally:
            cli.Gst.ElementFactory.make.side_effect = None
        c.pipeline.add.assert_not_called()

    def test_upnp_can_be_explicitly_enabled(self):
        wb = MagicMock()
        cli.configure_upnp(wb, True)
        wb.get_property.return_value.get_property.return_value.set_property.assert_called_once_with("upnp", True)

    def test_reconnect_unkeys_the_radio_before_any_native_teardown(self):
        # Review finding: peers were torn down (native, potentially blocking)
        # before the radio was unkeyed, so a hung teardown left it transmitting.
        import asyncio

        async def scenario():
            ptt = cli.AiocPtt("fake-port")
            c = cli.WalkieClient("wss://x", "R", "src", "sink", None, "system", ptt=ptt)
            c.loop = asyncio.get_running_loop()
            c.mic_mute = MagicMock()
            c.radio_key()
            self.assertTrue(ptt.keyed)
            c.peers["p"] = cli.Peer("p", "P")
            seen = []
            c.remove_peer = lambda pid: seen.append(ptt.keyed)
            c.reset_for_reconnect()
            return seen, ptt.keyed

        seen, keyed_after = asyncio.run(scenario())
        self.assertEqual(seen, [False])
        self.assertFalse(keyed_after)

    def test_each_peer_gets_a_leaky_queue_between_tee_and_webrtcbin(self):
        c = cli.WalkieClient("wss://x", "R", "src", "sink", None, "system")
        c.pipeline = MagicMock()
        c.tee = MagicMock()
        made = []

        def make(factory, name=None):
            el = MagicMock(name=factory)
            el.factory = factory
            # webrtcbin.get_property("ice-agent").get_property("agent").get_property("upnp")
            el.get_property.return_value.get_property.return_value.get_property.return_value = False
            made.append(el)
            return el

        cli.Gst.ElementFactory.make.side_effect = make
        try:
            peer = cli.Peer("p1", "Dad")
            c.create_peer_connection(peer, initiator=False)
        finally:
            cli.Gst.ElementFactory.make.side_effect = None
        queues = [e for e in made if e.factory == "queue"]
        self.assertEqual(len(queues), 1)
        queues[0].set_property.assert_any_call("leaky", 2)
        self.assertIs(peer.send_queue, queues[0])
        # tee pad feeds the queue, not webrtcbin directly
        c.tee.get_request_pad.return_value.link.assert_called_once_with(queues[0].get_static_pad.return_value)

    def test_negotiation_listener_is_attached_before_the_element_joins_the_pipeline(self):
        # Regression: connecting it after pipeline.add()/sync_state_with_parent()
        # missed webrtcbin's one-shot "negotiation needed", so a gateway that had
        # to start the connection (its id sorts lower) never sent an offer.
        c = cli.WalkieClient("wss://x", "R", "src", "sink", None, "system")
        c.pipeline = MagicMock()
        c.tee = MagicMock()
        order = []

        def make(factory, name=None):
            el = MagicMock(name=factory)
            el.factory = factory
            el.get_property.return_value.get_property.return_value.get_property.return_value = False
            if factory == "webrtcbin":
                el.connect.side_effect = lambda signal, *a: order.append(("connect", signal))
            return el

        c.pipeline.add.side_effect = lambda el: order.append(("add", el.factory))
        cli.Gst.ElementFactory.make.side_effect = make
        try:
            c.create_peer_connection(cli.Peer("p1", "Dad"), initiator=True)
        finally:
            cli.Gst.ElementFactory.make.side_effect = None
        self.assertLess(order.index(("connect", "on-negotiation-needed")), order.index(("add", "webrtcbin")))
        self.assertLess(order.index(("connect", "on-ice-candidate")), order.index(("add", "webrtcbin")))

    def test_remove_peer_releases_the_queue(self):
        c = cli.WalkieClient("wss://x", "R", "src", "sink", None, "system")
        c.pipeline, c.tee, c.mixer = MagicMock(), MagicMock(), MagicMock()
        peer = cli.Peer("p1", "Dad")
        peer.webrtcbin, peer.tee_pad, peer.send_queue = MagicMock(), MagicMock(), MagicMock()
        wb, queue = peer.webrtcbin, peer.send_queue
        peer.webrtc_sink_pad = sink_pad = MagicMock()
        peer.signal_handlers = [11, 12, 13]
        c.peers["p1"] = peer
        c.remove_peer("p1")
        queue.set_state.assert_called_once()
        c.pipeline.remove.assert_any_call(queue)
        wb.release_request_pad.assert_called_once_with(sink_pad)
        self.assertEqual(wb.disconnect.call_count, 3)
        self.assertEqual(peer.signal_handlers, [])
        self.assertIsNone(peer.webrtcbin)
        self.assertIsNone(peer.send_queue)
        self.assertIsNone(peer.webrtc_sink_pad)
        c.remove_peer("p1")  # repeated removal must not release pads twice
        wb.release_request_pad.assert_called_once()


class DiagnosticsTests(unittest.TestCase):
    def test_sdp_lines_are_logged_only_in_debug_mode(self):
        import io
        from contextlib import redirect_stderr

        sdp = (
            "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
            "a=rtpmap:111 opus/48000/2\r\na=sendrecv\r\na=ice-ufrag:x\r\n"
        )
        quiet = cli.WalkieClient("wss://x", "R", "s", "k", None, "system")
        loud = cli.WalkieClient("wss://x", "R", "s", "k", None, "system", vox_debug=True)
        buf = io.StringIO()
        with redirect_stderr(buf):
            quiet._log_sdp("remote offer", sdp)
        self.assertEqual(buf.getvalue(), "")
        with redirect_stderr(buf):
            loud._log_sdp("remote offer", sdp)
        out = buf.getvalue()
        self.assertIn("a=sendrecv", out)
        self.assertIn("a=rtpmap:111 opus/48000/2", out)
        self.assertNotIn("ice-ufrag", out)

    def test_debug_status_never_polls_webrtc_stats(self):
        # Regression: polling webrtcbin "get-stats" every few seconds per peer
        # coincided with the gateway growing to 3.4 GB and being OOM-killed.
        # The status line must read memory from /proc instead.
        import io
        from contextlib import redirect_stderr

        c = cli.WalkieClient("wss://x", "R", "s", "k", None, "system", vox_debug=True)
        c.pipeline = MagicMock()
        peer = cli.Peer("p1", "Dad")
        peer.webrtcbin = MagicMock()
        c.peers["p1"] = peer
        buf = io.StringIO()
        with redirect_stderr(buf):
            c._debug_status()
        peer.webrtcbin.emit.assert_not_called()
        self.assertIn("memory", buf.getvalue())

    def test_status_reports_queue_levels_python_objects_and_threads(self):
        import io
        from contextlib import redirect_stderr

        c = cli.WalkieClient("wss://x", "R", "s", "k", None, "system", vox_debug=True)
        c.pipeline = MagicMock()
        peer = cli.Peer("p1", "Dad")
        levels = {"current-level-buffers": 7, "current-level-bytes": 4096}
        peer.send_queue = MagicMock()
        peer.send_queue.get_property.side_effect = lambda name: levels[name]
        c.peers["p1"] = peer
        buf = io.StringIO()
        with redirect_stderr(buf):
            c._debug_status()
        out = buf.getvalue()
        self.assertIn("send-queue 7 buf/4 KB", out)
        self.assertIn("python objects", out)
        self.assertIn("threads", out)

    def test_rss_reading_is_an_integer(self):
        self.assertIsInstance(cli.WalkieClient._rss_mb(), int)

    def test_mixer_has_latency_slack(self):
        cli.Gst.parse_launch.reset_mock()
        c = cli.WalkieClient("wss://x", "R", "alsasrc", "alsasink", None, "system", vox=cli.Vox())
        c.build_pipeline()
        self.assertIn("audiomixer name=mix latency=200000000", cli.Gst.parse_launch.call_args[0][0])


class StatusLineTests(unittest.TestCase):
    def test_non_tty_prints_plain_lines_without_carriage_returns(self):
        import io
        from contextlib import redirect_stdout

        c = cli.WalkieClient("wss://x", "R", "src", "sink", None, "system")
        buf = io.StringIO()  # StringIO.isatty() is False, like a journal or a pipe
        with redirect_stdout(buf):
            c._status("[radio: TX keyed]")
            c._status("[ready]")
        self.assertEqual(buf.getvalue(), "[radio: TX keyed]\n[ready]\n")


class SafeTextTests(unittest.TestCase):
    def test_strips_control_and_surrogates(self):
        s = cli.safe_text("Dad\x1b[2J\r\nX\ud800Y")
        for bad in "\x1b\r\n\ud800":
            self.assertNotIn(bad, s)

    def test_keeps_normal_text(self):
        self.assertEqual(cli.safe_text("Mamá ✓"), "Mamá ✓")

    def test_caps_length(self):
        self.assertEqual(len(cli.safe_text("a" * 500, limit=10)), 10)


class AiocPttTests(unittest.TestCase):
    def test_idle_key_unkey_close(self):
        ptt = cli.AiocPtt("/dev/ttyACM0")
        ser = FakeSerial.instances[-1]
        self.assertFalse(ser.dtr)
        self.assertTrue(ser.rts)
        self.assertFalse(ptt.keyed)
        ptt.key()
        self.assertTrue(ser.dtr and not ser.rts and ptt.keyed)  # DTR=1 RTS=0
        ptt.unkey()
        self.assertFalse(ser.dtr)
        self.assertTrue(ser.rts)
        ptt.key()
        ptt.close()
        ptt.close()  # idempotent
        self.assertFalse(ser.is_open)
        self.assertFalse(ser.dtr)


def floor(holder, name=None, priority=False):
    return json.dumps({"type": "floor", "holder": holder, "name": name, "priority": priority})


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self, **kw):
        ptt = cli.AiocPtt("/dev/ttyACM9")
        c = cli.WalkieClient(
            "wss://x", "Radio", "src", "sink", None, "system",
            ptt=ptt, vox=cli.Vox(), ptt_tail_ms=50, max_ptt_seconds=1, **kw,
        )
        c.loop = asyncio.get_running_loop()
        c.mic_mute = MagicMock()
        c.my_id = "aaaa"
        c.sent = []

        async def fake_send(m):
            c.sent.append(m)

        c.send = fake_send
        return c, ptt

    async def test_keys_on_other_floor_and_unkeys_after_tail(self):
        c, ptt = self.make_client()
        await c.handle_message(floor("bbbb", "Dad"))
        self.assertTrue(ptt.keyed)
        await c.handle_message(floor(None))
        self.assertTrue(ptt.keyed)  # tail
        await asyncio.sleep(0.15)
        self.assertFalse(ptt.keyed)

    async def test_own_floor_unkeys_immediately(self):
        c, ptt = self.make_client()
        await c.handle_message(floor("bbbb", "Dad"))
        await c.handle_message(floor("aaaa", "Radio"))
        self.assertFalse(ptt.keyed)
        self.assertTrue(c.have_floor)

    async def test_taking_the_floor_does_not_lock_out_vox(self):
        # Regression: the gateway granted itself the floor for radio->LAN audio,
        # the "make sure the radio is unkeyed" step blocked VOX for 0.5 s, VOX
        # reported "stop", and the floor was released again immediately.
        c, ptt = self.make_client()
        c.vox.active = True
        await c.handle_message(floor("aaaa", "Radio"))
        self.assertEqual(c._vox_blocked_until, 0.0)
        self.assertEqual(c.vox.update(-10, 20, blocked=False), None)  # still active, not stopped
        self.assertTrue(c.vox.active)

    async def test_real_unkey_still_blocks_vox_briefly(self):
        c, ptt = self.make_client()
        await c.handle_message(floor("bbbb", "Dad"))
        self.assertTrue(ptt.keyed)
        await c.handle_message(floor(None))
        await asyncio.sleep(0.15)
        self.assertFalse(ptt.keyed)
        self.assertGreater(c._vox_blocked_until, 0.0)

    async def test_watchdog_unkeys(self):
        c, ptt = self.make_client()
        await c.handle_message(floor("bbbb", "Dad"))
        await asyncio.sleep(1.2)
        self.assertFalse(ptt.keyed)

    async def test_reconnect_reset_unkeys(self):
        c, ptt = self.make_client()
        await c.handle_message(floor("bbbb", "Dad"))
        c.reset_for_reconnect()
        self.assertFalse(ptt.keyed)

    async def test_stale_floor_keeps_pending_request(self):
        c, _ = self.make_client()
        c.talking, c.have_floor = True, False
        await c.handle_message(floor("bbbb", "Dad"))
        self.assertTrue(c.talking)
        await c.handle_message(json.dumps({"type": "floor-denied", "name": "Dad"}))
        self.assertFalse(c.talking)

    async def test_losing_a_held_floor_clears_intent(self):
        c, _ = self.make_client()
        c.talking, c.have_floor = True, True
        await c.handle_message(floor("bbbb", "Dad"))
        self.assertFalse(c.talking)
        self.assertFalse(c.have_floor)

    async def test_token_only_on_verified_connection(self):
        c, _ = self.make_client()
        c.token = "secret-token-value"
        c.verified = False
        await c.handle_message(json.dumps({"type": "welcome", "id": "aaaa"}))
        self.assertIsNone(c.sent[-1]["token"])
        c.verified = True
        await c.handle_message(json.dumps({"type": "welcome", "id": "aaaa"}))
        self.assertEqual(c.sent[-1]["token"], "secret-token-value")

    # ---------- roger beep ----------

    def make_beep_client(self, roger=True, keyed_gateway=True):
        c, ptt = self.make_client(beep_lead_ms=100)
        c.beep_gate = MagicMock()
        c.beep_src = MagicMock()
        c.roger_beep = roger
        if keyed_gateway:
            ptt.key()
        return c, ptt

    async def test_welcome_sets_the_beep_switch_from_the_server(self):
        c, _ = self.make_client()
        await c.handle_message(json.dumps({"type": "welcome", "id": "aaaa", "rogerBeep": True}))
        self.assertTrue(c.roger_beep)
        await c.handle_message(json.dumps({"type": "welcome", "id": "aaaa", "rogerBeep": False}))
        self.assertFalse(c.roger_beep)
        # An older server that does not send the field means off, as does a non-boolean.
        await c.handle_message(json.dumps({"type": "welcome", "id": "aaaa"}))
        self.assertFalse(c.roger_beep)
        await c.handle_message(json.dumps({"type": "welcome", "id": "aaaa", "rogerBeep": "yes"}))
        self.assertFalse(c.roger_beep)

    async def test_beep_plays_when_a_speaker_releases_and_holds_the_key_for_it(self):
        c, ptt = self.make_beep_client()
        await c.handle_message(floor("bbbb", "Dad"))
        await c.handle_message(floor(None))
        # tail (50 ms) + the whole beep: still keyed well after the plain tail has passed
        await asyncio.sleep(0.15)
        self.assertTrue(ptt.keyed)
        c.beep_gate.set_property.assert_any_call("mute", False)
        # still keyed after the beep itself is over: the lead covers audio-buffer delay
        await asyncio.sleep(cli.BEEP_MS / 1000.0 - 0.1)
        self.assertTrue(ptt.keyed)
        await asyncio.sleep(0.3)
        self.assertFalse(ptt.keyed)
        c.beep_gate.set_property.assert_called_with("mute", True)

    async def test_beep_plays_every_note_in_order_and_ends_muted(self):
        c, _ = self.make_beep_client()
        await c.handle_message(floor("bbbb", "Dad"))
        await c.handle_message(floor(None))
        await asyncio.sleep(cli.BEEP_MS / 1000.0 + 0.1)
        freqs = [call.args[1] for call in c.beep_src.set_property.call_args_list if call.args[0] == "freq"]
        self.assertEqual(freqs, [float(f) for f, _ in cli.BEEP_PATTERN])
        self.assertEqual(c.beep_gate.set_property.call_args_list[-1].args, ("mute", True))
        self.assertEqual(cli.BEEP_MS, sum(ms for _, ms in cli.BEEP_PATTERN))
        # The warble is continuous: the gate opens at the first note and closes once, at the end.
        mutes = [call.args[1] for call in c.beep_gate.set_property.call_args_list if call.args[0] == "mute"]
        self.assertEqual(mutes[-1], True)
        self.assertEqual(mutes.count(True), 1)

    async def test_no_beep_when_switched_off(self):
        c, ptt = self.make_beep_client(roger=False)
        await c.handle_message(floor("bbbb", "Dad"))
        await c.handle_message(floor(None))
        c.beep_gate.set_property.assert_not_called()
        await asyncio.sleep(0.15)
        self.assertFalse(ptt.keyed)  # unkeys after the normal tail, not a longer one

    async def test_no_beep_for_a_handover_between_speakers(self):
        c, _ = self.make_beep_client()
        await c.handle_message(floor("bbbb", "Dad"))
        await c.handle_message(floor("cccc", "Mum", priority=True))
        c.beep_gate.set_property.assert_not_called()

    async def test_no_beep_on_the_first_floor_message_or_after_a_reconnect(self):
        c, _ = self.make_beep_client()
        await c.handle_message(floor(None))  # nobody held it before
        c.beep_gate.set_property.assert_not_called()
        await c.handle_message(floor("bbbb", "Dad"))
        c.reset_for_reconnect()
        c.beep_gate.set_property.reset_mock()
        await c.handle_message(floor(None))
        c.beep_gate.set_property.assert_not_called()

    async def test_gateway_does_not_beep_when_the_radio_is_not_keyed(self):
        # The radio user held the floor: nothing is being relayed to the radio.
        c, _ = self.make_beep_client(keyed_gateway=False)
        await c.handle_message(floor("aaaa", "Radio"))
        await c.handle_message(floor(None))
        c.beep_gate.set_property.assert_not_called()

    async def test_the_log_says_what_the_beep_did(self):
        c, _ = self.make_beep_client()
        c._status = MagicMock()
        await c.handle_message(floor("bbbb", "Dad"))
        await c.handle_message(floor(None))
        c._status.assert_any_call("[roger beep]")

        c2, _ = self.make_beep_client(keyed_gateway=False)
        c2._status = MagicMock()
        await c2.handle_message(floor("aaaa", "Radio"))
        await c2.handle_message(floor(None))
        c2._status.assert_any_call("[roger beep skipped: the radio was not keyed]")

        c3, _ = self.make_beep_client()
        c3.beep_gate = None
        c3._status = MagicMock()
        await c3.handle_message(floor("bbbb", "Dad"))
        await c3.handle_message(floor(None))
        c3._status.assert_any_call("[roger beep unavailable on this machine]")

    async def test_missing_beep_branch_is_harmless(self):
        c, ptt = self.make_beep_client()
        c.beep_gate = None
        c.beep_src = None
        await c.handle_message(floor("bbbb", "Dad"))
        await c.handle_message(floor(None))
        await asyncio.sleep(0.15)
        self.assertFalse(ptt.keyed)  # plain tail, no extension

    async def test_reconnect_silences_a_playing_beep(self):
        c, _ = self.make_beep_client()
        c.reset_for_reconnect()
        c.beep_gate.set_property.assert_called_with("mute", True)

    def test_failed_beep_branch_leaves_the_rest_of_the_pipeline_alone(self):
        made = cli.Gst.ElementFactory.make.return_value
        made.get_static_pad.return_value.link.return_value = cli.Gst.PadLinkReturn.OK
        c = cli.WalkieClient("wss://x", "R", "alsasrc", "alsasink", None, "system")
        c.build_pipeline()
        self.assertIsNotNone(c.beep_gate)
        # An element that cannot be created disables the beep, nothing else.
        cli.Gst.ElementFactory.make.return_value = None
        try:
            c.beep_gate = None
            c._build_beep_branch()
            self.assertIsNone(c.beep_gate)
        finally:
            cli.Gst.ElementFactory.make.return_value = made

    async def test_non_object_messages_ignored(self):
        c, _ = self.make_client()
        for raw in ("null", "[]", '"x"', "42"):
            await c.handle_message(raw)

    async def test_server_identity_modes(self):
        c, _ = self.make_client()

        class Ssl:
            def getpeercert(self, binary_form=False):
                return b"cert-bytes"

        ws = types.SimpleNamespace(transport=types.SimpleNamespace(get_extra_info=lambda k: Ssl()))
        c.url, c.tls_mode, c.pin = "wss://x", "pin", cli.sha256_pin(b"cert-bytes")
        self.assertTrue(c.check_server_identity(ws))
        c.pin = "0" * 64
        with self.assertRaises(ConnectionError):
            c.check_server_identity(ws)
        c.tls_mode = "insecure"
        self.assertFalse(c.check_server_identity(ws))
        c.url, c.tls_mode = "ws://x", "system"
        self.assertFalse(c.check_server_identity(ws))


if __name__ == "__main__":
    unittest.main()
