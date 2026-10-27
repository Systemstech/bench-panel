#!/usr/bin/env python3
"""A pretend SCPI instrument on a raw TCP socket -- for trying the panel with no hardware.

    python3 tools/sim_scpi.py --model 34401A --port 5025
    python3 tools/sim_scpi.py --model E3641A --port 5026

Then, in the dashboard: Instruments -> Add -> connected by "Other VISA resource"
    TCPIP0::127.0.0.1::5025::SOCKET

WHAT IT IS NOT
--------------
It is not a model of any instrument. It answers *IDN? with the model you give it,
remembers whatever you set ("VOLT 5" makes "VOLT?" answer 5), answers queries it
has never seen with 0, and returns a slowly wandering number for READ? and MEAS:*?.
Good enough to exercise the loader, the Instruments manager, the power buttons and
a card's wiring. It proves NOTHING about a driver's command strings -- only real
hardware (or a recorded trace) can do that.
"""
import argparse
import random
import socketserver
import threading

LOCK = threading.Lock()
STATE = {}
MODEL = "SIM"

DEFAULTS = {
    "SYST:ERR": '+0,"No error"',
    "*OPC": "1",
    "OUTP": "0", "OUTP:STAT": "0", "OUTP1": "0", "OUTP2": "0",
    "VOLT:RANG": "P35V",
    "STAT:QUES:COND": "0",
    "MEM:STAT:NAME": '"STATE"',
    "MEM:STAT:VAL": "0",
}


def _key(cmd):
    head = cmd.split(None, 1)[0].upper().rstrip("?")
    return head


def answer(line):
    line = line.strip()
    if not line:
        return None
    if line.upper() == "*IDN?":
        return f"SIMULATED,{MODEL},SIM0001,0.1"
    if line.endswith("?") or "? " in line:
        k = _key(line)
        if k in ("READ", "FETC") or k.startswith("MEAS"):
            return f"{random.gauss(1.0, 0.0005):+.8E}"
        with LOCK:
            return STATE.get(k, DEFAULTS.get(k, "0"))
    parts = line.split(None, 1)
    with LOCK:
        STATE[_key(parts[0])] = parts[1] if len(parts) > 1 else "1"
    return None


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        for raw in self.rfile:
            for cmd in raw.decode(errors="replace").split(";"):
                out = answer(cmd)
                if out is not None:
                    self.wfile.write((out + "\n").encode())


def main():
    global MODEL
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", required=True, help="what *IDN? reports, e.g. 34401A")
    ap.add_argument("--port", type=int, default=5025)
    a = ap.parse_args()
    MODEL = a.model
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", a.port), Handler) as srv:
        print(f"simulated {MODEL} on TCPIP0::127.0.0.1::{a.port}::SOCKET")
        srv.serve_forever()


if __name__ == "__main__":
    main()
