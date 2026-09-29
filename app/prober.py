#!/usr/bin/env python3
"""
Fast ICMP prober for convergence tests. Run inside a lab node's network
namespace (nsenter -n), with the host's python:

    prober.py SRC DST INTERVAL_MS DURATION_S

Sends one echo request every INTERVAL_MS from SRC to DST for DURATION_S (or
until SIGTERM) and records each reply's round-trip time. While it runs it prints a progress line
every 0.5 s: {"p":1,"t":s,"sent":n,"recv":n,"run":current consecutive losses}.
At the end it prints one line {"done":1,"start":epoch,"interval_ms":..,
"rtt_us":[rtt in microseconds, or -1 for a lost probe, per sequence number]}.
"""

import json
import os
import signal
import socket
import struct
import sys
import threading
import time


def checksum(b):
    if len(b) % 2:
        b += b"\0"
    s = sum(struct.unpack("!%dH" % (len(b) // 2), b))
    s = (s >> 16) + (s & 0xffff)
    s += s >> 16
    return ~s & 0xffff


def main():
    src, dst = sys.argv[1], sys.argv[2]
    interval = max(1.0, float(sys.argv[3])) / 1000.0
    duration = float(sys.argv[4])
    n = int(duration / interval)
    ident = os.getpid() & 0xffff
    sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
    sock.bind((src, 0))
    sock.settimeout(0.2)
    sent = [0.0] * n
    rtt = [-1] * n
    stop = threading.Event()

    def rx():
        while not stop.is_set():
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            now = time.monotonic()
            ihl = (data[0] & 0x0f) * 4
            if len(data) < ihl + 8:
                continue
            typ, code, _, rid, seq = struct.unpack("!BBHHH", data[ihl:ihl + 8])
            if typ != 0 or rid != ident or addr[0] != dst:
                continue
            # the 16-bit sequence wraps; map it back to the closest index sent
            base = int((now - t0) / interval) if sent else 0
            idx = seq + ((base - seq + 32768) // 65536) * 65536
            if 0 <= idx < n and sent[idx] and rtt[idx] < 0:
                rtt[idx] = int((now - sent[idx]) * 1e6)

    t0 = time.monotonic()
    start_epoch = time.time()
    th = threading.Thread(target=rx, daemon=True)
    th.start()
    last_report = 0.0
    payload = b"clab-dashboard-convergence-probe"
    # SIGTERM / SIGINT end the run early and still print the results: the
    # caller does not know in advance how long its failure steps take
    halt = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: halt.set())
    signal.signal(signal.SIGINT, lambda *a: halt.set())
    last = n
    for i in range(n):
        if halt.is_set():
            last = i
            break
        target = t0 + i * interval
        d = target - time.monotonic()
        if d > 0:
            time.sleep(d)
        hdr = struct.pack("!BBHHH", 8, 0, 0, ident, i & 0xffff)
        pkt = hdr + payload
        pkt = struct.pack("!BBHHH", 8, 0, checksum(pkt), ident, i & 0xffff) + payload
        sent[i] = time.monotonic()
        try:
            sock.sendto(pkt, (dst, 0))
        except OSError:
            pass                            # no route right now: counts as lost
        el = sent[i] - t0
        if el - last_report >= 0.5:
            last_report = el
            got = sum(1 for x in rtt[:i + 1] if x >= 0)
            run = 0
            # consecutive losses among probes old enough to have been answered
            j = i - int(0.2 / interval)
            while j >= 0 and rtt[j] < 0:
                run += 1
                j -= 1
            print(json.dumps({"p": 1, "t": round(el, 2), "sent": i + 1, "recv": got,
                              "run_ms": round(run * interval * 1000)}), flush=True)
    time.sleep(1.0)                          # stragglers
    stop.set()
    print(json.dumps({"done": 1, "start": start_epoch, "interval_ms": interval * 1000,
                      "rtt_us": rtt[:last]}), flush=True)


if __name__ == "__main__":
    main()
