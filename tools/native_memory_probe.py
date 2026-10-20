"""Run one existing leak-probe variant with Linux/glibc heap telemetry.

Use --trim to test whether malloc_trim releases retained free pages. This is
diagnostic only; it is not a production memory-management workaround.
"""
import argparse
import ctypes
import os
from pathlib import Path
import sys
import threading
import time


class Mallinfo2(ctypes.Structure):
    _fields_ = [(name, ctypes.c_size_t) for name in (
        "arena", "ordblks", "smblks", "hblks", "hblkhd", "usmblks",
        "fsmblks", "uordblks", "fordblks", "keepcost")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--variant", choices=("unlinked", "queue", "depay", "decode", "app"), default="app")
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--trim", action="store_true")
    parser.add_argument("--disable-upnp", action="store_true")
    args = parser.parse_args()
    if not 5 <= args.seconds <= 300:
        parser.error("seconds must be between 5 and 300")
    libc = ctypes.CDLL("libc.so.6")
    libc.mallinfo2.restype = Mallinfo2
    libc.malloc_trim.argtypes = [ctypes.c_size_t]
    libc.malloc_trim.restype = ctypes.c_int

    # Support the original ea78d62 probe without executing its five-run loop.
    path = args.repo / "tools" / "leak_probe.py"
    source = path.read_text().split('\nfor label, tail in (', 1)[0]
    if args.disable_upnp:
        source = source.replace('rw = remote.get_by_name("rw")',
                                'rw = remote.get_by_name("rw")\n    disable_upnp(rw)')
    sys.path.insert(0, str(args.repo / "cli"))
    scope = {"__file__": str(path), "__name__": "audit_probe"}

    def disable_upnp(wb):
        # Keep the GI wrapper alive: old GstWebRTCICE builds expose a floating
        # GstObject, whose temporary wrapper can otherwise destroy the agent.
        wb._audit_ice = wb.get_property("ice-agent")
        agent = wb._audit_ice.get_property("agent")
        agent.set_property("upnp", False)
        print("UPNP disabled: %s" % wb.get_name(), flush=True)

    scope["disable_upnp"] = disable_upnp
    exec(compile(source, str(path), "exec"), scope)
    if args.disable_upnp:
        client = scope["cli"].WalkieClient
        original = client.create_peer_connection

        def create(self, *a, **kw):
            wb = original(self, *a, **kw)
            disable_upnp(wb)
            return wb

        client.create_peer_connection = create
    stop = threading.Event()
    started = time.monotonic()

    def sample():
        fields = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
        heap = libc.mallinfo2()
        print("MEM t=%.1f rss_kb=%s native_threads=%s allocated=%d free=%d mmap=%d" % (
            time.monotonic() - started, fields["VmRSS"].strip(), fields["Threads"].strip(),
            heap.uordblks, heap.fordblks, heap.hblkhd), flush=True)

    def monitor():
        while not stop.wait(5):
            sample()
            if args.trim:
                libc.malloc_trim(0)
                sample()

    print("PID=%d variant=%s trim=%s" % (os.getpid(), args.variant, args.trim), flush=True)
    sample()
    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    try:
        scope["run_variant"](args.variant, args.variant, args.seconds)
    finally:
        stop.set()
        thread.join()
        sample()


if __name__ == "__main__":
    main()
