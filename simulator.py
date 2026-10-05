#!/usr/bin/env python3
"""Simulator backend for the picoammeter readout - no hardware needed.

Writes runtime/status.txt once a second, cycling WARMUP -> SETTLE -> ZERO ->
MEASURE with a simulated current, temperature, stability and overload. Consumes
runtime/command.txt (ZERO / SET_GAIN / AUTORANGE / SET_ALARM_* / CLEAR) with the
same auth check the real device expects.

Run it beside the frontend, or let the frontend start it in a background thread.
"""
import math
import os
import random
import tempfile
import threading
import time

HERE     = os.path.dirname(os.path.abspath(__file__))
RUNTIME  = os.path.join(HERE, "runtime")
STATUS   = os.path.join(RUNTIME, "status.txt")
COMMAND  = os.path.join(RUNTIME, "command.txt")
PASSWORD = "admin"

# compressed demo durations (seconds)
WARMUP, SETTLE, ZERO = 3, 3, 2


def atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    with os.fdopen(fd, "w") as f:
        f.write(text)
    # On Windows os.replace fails if the reader has the file open; retry briefly.
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                os.remove(tmp)
                return
            time.sleep(0.05)


def read_command():
    try:
        with open(COMMAND) as f:
            text = f.read()
    except OSError:
        return None
    os.remove(COMMAND)
    d = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def run(stop=None):
    """Write status once a second until `stop` (an Event) is set."""
    gain_exp = 8
    autorange = False
    alarm_high = alarm_low = None
    alarm = "OK"
    last_cmd, last_res = "-", "-"
    rezero_until = 0.0
    t0 = time.time()

    while stop is None or not stop.is_set():
        now = time.time()
        el = now - t0

        if   el < WARMUP:                 state = "WARMUP"
        elif el < WARMUP + SETTLE:        state = "SETTLE"
        elif el < WARMUP + SETTLE + ZERO: state = "ZERO"
        else:                             state = "MEASURE"

        c = read_command()
        if c:
            cmd = c.get("cmd", "")
            last_cmd = cmd
            if c.get("auth") != PASSWORD:
                last_res = "DENIED"
            else:
                last_res = "OK"
                if cmd == "ZERO":
                    rezero_until = now + ZERO
                elif cmd.startswith("SET_GAIN "):
                    try:
                        e = int(cmd.split()[1])
                        if 4 <= e <= 13:
                            gain_exp, autorange = e, False
                        else:
                            last_res = "BADARG"
                    except ValueError:
                        last_res = "BADARG"
                elif cmd == "AUTORANGE ON":
                    autorange = True
                elif cmd == "AUTORANGE OFF":
                    autorange = False
                elif cmd.startswith("SET_ALARM_HIGH "):
                    alarm_high = float(cmd.split()[1])
                elif cmd.startswith("SET_ALARM_LOW "):
                    alarm_low = float(cmd.split()[1])
                elif cmd == "CLEAR":
                    alarm = "OK"
                else:
                    last_res = "BADARG"

        # simulated signal (fA): slow swing + noise + rare spike
        base  = 40.0 + 10.0 * math.sin(el / 20.0)
        noise = random.gauss(0, 2.0 if state == "MEASURE" else 6.0)
        spike = 60.0 if random.random() < 0.02 else 0.0
        current_fa = base + noise + spike
        mean_v = current_fa * (10.0 ** (gain_exp - 15))   # inverse fA conversion

        if state == "MEASURE":
            stability = "STABLE" if abs(noise) < 4 else "UNSTABLE"
        else:
            stability = "SETTLING"

        temp = 40.0 + 2.0 * math.sin(el / 30.0) + random.gauss(0, 0.1)
        overload = 1 if current_fa > 1e5 else 0

        alarm = "OK"
        if alarm_high is not None and current_fa > alarm_high:
            alarm = "HIGH"
        elif alarm_low is not None and current_fa < alarm_low:
            alarm = "LOW"

        backend = "RUNNING"
        note = " (re-zeroing)" if now < rezero_until else ""
        atomic_write(STATUS,
            "schema=1\nepoch_ms=%d\nbackend=%s\nstate=%s\ngain_exp=%d\n"
            "current_fA=%.7g\nmean_V=%.7g\nstability=%s\noverload=%d\n"
            "cpu_temp_c=%.1f\nalarm=%s\nlast_cmd=%s\nlast_cmd_result=%s\n" % (
                int(now * 1000), backend, state + note, gain_exp, current_fa,
                mean_v, stability, overload, temp, alarm, last_cmd, last_res))

        if stop is not None and stop.wait(1.0):
            break
        if stop is None:
            time.sleep(1.0)


def start_in_thread():
    """Start the simulator on a daemon thread; returns the stop Event."""
    stop = threading.Event()
    t = threading.Thread(target=run, args=(stop,), daemon=True)
    t.start()
    return stop


if __name__ == "__main__":
    print("simulator running (writes %s); Ctrl+C to stop" % STATUS)
    try:
        run()
    except KeyboardInterrupt:
        print("\nstopped")
