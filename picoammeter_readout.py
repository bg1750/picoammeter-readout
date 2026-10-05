#!/usr/bin/env python3
"""Picoammeter operator readout (Tkinter, standard library only).

Independent of the backend by design: it only reads runtime/status.txt and
writes runtime/command.txt. It never opens the serial port and never calls the
backend, so it keeps running (showing STALE) if the backend is down.

Covers the operator functions:
  1 password gate on every settings change
  2 zeroing (Zero button -> cmd=ZERO)
  3 gain adjustment / autorange (SET_GAIN / AUTORANGE)
  4 current + temperature, with optional temp correction
  5 alarm levels (high/low fA), evaluated frontend-side so alarms work even if
    the backend does not
  6 front panel display: large current readout + state/stability/connection

This build starts a software simulator in the background, so it runs with no
hardware attached.
"""
import hashlib
import json
import os
import sys
import tempfile
import time
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

HERE         = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DIR  = os.path.join(HERE, "runtime")
STATUS_PATH  = os.path.join(RUNTIME_DIR, "status.txt")
COMMAND_PATH = os.path.join(RUNTIME_DIR, "command.txt")
CONFIG_PATH  = os.path.join(HERE, "frontend_config.json")

POLL_MS          = 250      # how often we re-read status.txt
STALE_MS         = 3000     # heartbeat older than this -> STALE
DEFAULT_PASSWORD = "admin"  # change on first run; stored hashed in config

# Amplifier gain drift with temperature, %/degC. Used only if temp correction
# is enabled.
DEFAULT_DRIFT_PCT_PER_C = 0.03


# ----------------------------------------------------------------------------
# config (password hash + salt, alarm levels, temp-correction settings)
# ----------------------------------------------------------------------------
def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    salt = os.urandom(8).hex()
    cfg = {
        "salt": salt,
        "pw_hash": _hash(DEFAULT_PASSWORD, salt),
        "alarm_high_fa": "",
        "alarm_low_fa": "",
        "temp_correct": False,
        "drift_pct_per_c": DEFAULT_DRIFT_PCT_PER_C,
        "t_ref_c": "",
    }
    save_config(cfg)
    return cfg


def save_config(cfg):
    _atomic_write(CONFIG_PATH, json.dumps(cfg, indent=2))


def _hash(password, salt):
    return hashlib.sha256((salt + password).encode()).hexdigest()


# ----------------------------------------------------------------------------
# file helpers
# ----------------------------------------------------------------------------
def _atomic_write(path, text):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def read_status():
    """Returns (dict, age_ms) or (None, None) if no status file."""
    try:
        with open(STATUS_PATH) as f:
            text = f.read()
    except OSError:
        return None, None
    d = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()
    age = None
    try:
        age = int(time.time() * 1000) - int(d.get("epoch_ms", "0"))
    except ValueError:
        pass
    return d, age


# ----------------------------------------------------------------------------
# current formatting: fA -> auto-scaled string
# ----------------------------------------------------------------------------
def format_current(fa):
    if fa is None:
        return "--"
    a = abs(fa)
    if a < 1e3:
        return f"{fa:.2f} fA"
    if a < 1e6:
        return f"{fa/1e3:.3f} pA"
    if a < 1e9:
        return f"{fa/1e6:.3f} nA"
    if a < 1e12:
        return f"{fa/1e9:.3f} µA"
    return f"{fa/1e12:.3f} mA"


# ----------------------------------------------------------------------------
# the app
# ----------------------------------------------------------------------------
class Frontend:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.unlocked = False
        self.last_cmd_id = None

        root.title("Picoammeter Readout")
        root.minsize(560, 520)

        self._build_panel()
        self._build_controls()
        self._build_footer()
        self._apply_lock_state()
        self.poll()

    # ---- layout ----
    def _build_panel(self):
        panel = ttk.Frame(self.root, padding=12)
        panel.pack(fill="x")

        self.current_var = tk.StringVar(value="--")
        self.current_lbl = tk.Label(panel, textvariable=self.current_var,
                                    font=("Consolas", 40, "bold"))
        self.current_lbl.pack()

        self.corrected_var = tk.StringVar(value="")
        tk.Label(panel, textvariable=self.corrected_var,
                 font=("Consolas", 11)).pack()

        grid = ttk.Frame(self.root, padding=(12, 0))
        grid.pack(fill="x")
        self.field = {}
        rows = [("state", "State"), ("stability", "Stability"),
                ("gain", "Gain (V/A)"), ("temp", "Temperature"),
                ("overload", "Overload"), ("alarm", "Alarm"),
                ("conn", "Connection")]
        for i, (key, label) in enumerate(rows):
            ttk.Label(grid, text=label + ":", width=14, anchor="e").grid(
                row=i, column=0, sticky="e", padx=4, pady=1)
            var = tk.StringVar(value="--")
            self.field[key] = var
            ttk.Label(grid, textvariable=var, anchor="w",
                      font=("Consolas", 11)).grid(row=i, column=1, sticky="w")

    def _build_controls(self):
        box = ttk.LabelFrame(self.root, text="Controls", padding=10)
        box.pack(fill="x", padx=12, pady=10)

        # zeroing (2) + gain (3)
        self.zero_btn = ttk.Button(box, text="Zero now", command=self.do_zero)
        self.zero_btn.grid(row=0, column=0, padx=4, pady=4, sticky="w")

        ttk.Label(box, text="Gain 10^").grid(row=0, column=1, sticky="e")
        self.gain_var = tk.IntVar(value=8)
        self.gain_spin = ttk.Spinbox(box, from_=4, to=13, width=4,
                                     textvariable=self.gain_var)
        self.gain_spin.grid(row=0, column=2, sticky="w")
        self.gain_btn = ttk.Button(box, text="Set gain", command=self.do_set_gain)
        self.gain_btn.grid(row=0, column=3, padx=4)
        self.auto_btn = ttk.Button(box, text="Autorange ON",
                                   command=self.do_autorange)
        self.auto_btn.grid(row=0, column=4, padx=4)

        # alarms (5)
        ttk.Label(box, text="Alarm high fA").grid(row=1, column=0, sticky="e")
        self.alarm_high = tk.StringVar(value=self.cfg.get("alarm_high_fa", ""))
        ttk.Entry(box, width=10, textvariable=self.alarm_high).grid(
            row=1, column=1, sticky="w")
        ttk.Label(box, text="low fA").grid(row=1, column=2, sticky="e")
        self.alarm_low = tk.StringVar(value=self.cfg.get("alarm_low_fa", ""))
        ttk.Entry(box, width=10, textvariable=self.alarm_low).grid(
            row=1, column=3, sticky="w")
        self.alarm_btn = ttk.Button(box, text="Set alarms",
                                    command=self.do_set_alarms)
        self.alarm_btn.grid(row=1, column=4, padx=4, pady=4)

        # temperature correction (4)
        self.temp_correct = tk.BooleanVar(value=self.cfg.get("temp_correct", False))
        self.temp_chk = ttk.Checkbutton(
            box, text="Temp correct", variable=self.temp_correct,
            command=self.do_temp_toggle)
        self.temp_chk.grid(row=2, column=0, sticky="w", pady=4)
        self.tref_btn = ttk.Button(box, text="Set T_ref = now",
                                   command=self.do_set_tref)
        self.tref_btn.grid(row=2, column=1, sticky="w")
        self.tref_var = tk.StringVar(value=self._tref_text())
        ttk.Label(box, textvariable=self.tref_var).grid(
            row=2, column=2, columnspan=3, sticky="w")

        self.settings_widgets = [self.zero_btn, self.gain_spin, self.gain_btn,
                                 self.auto_btn, self.alarm_btn, self.temp_chk,
                                 self.tref_btn]

    def _build_footer(self):
        foot = ttk.Frame(self.root, padding=12)
        foot.pack(fill="x", side="bottom")
        self.lock_btn = ttk.Button(foot, text="Unlock", command=self.toggle_lock)
        self.lock_btn.pack(side="left")
        ttk.Button(foot, text="Change password",
                   command=self.change_password).pack(side="left", padx=6)
        self.msg = tk.StringVar(value="Locked. Unlock to change settings.")
        ttk.Label(foot, textvariable=self.msg).pack(side="right")

    # ---- password gate (1) ----
    def toggle_lock(self):
        if self.unlocked:
            self.unlocked = False
        else:
            pw = simpledialog.askstring("Unlock", "Password:", show="*",
                                        parent=self.root)
            if pw is None:
                return
            if _hash(pw, self.cfg["salt"]) != self.cfg["pw_hash"]:
                messagebox.showerror("Unlock", "Wrong password.")
                return
            self.unlocked = True
        self._apply_lock_state()

    def change_password(self):
        pw = simpledialog.askstring("Change password", "Current password:",
                                    show="*", parent=self.root)
        if pw is None:
            return
        if _hash(pw, self.cfg["salt"]) != self.cfg["pw_hash"]:
            messagebox.showerror("Change password", "Wrong password.")
            return
        new = simpledialog.askstring("Change password", "New password:",
                                     show="*", parent=self.root)
        if not new:
            return
        self.cfg["pw_hash"] = _hash(new, self.cfg["salt"])
        save_config(self.cfg)
        messagebox.showinfo("Change password", "Password changed.")

    def _apply_lock_state(self):
        state = "normal" if self.unlocked else "disabled"
        for w in self.settings_widgets:
            w.configure(state=state)
        self.lock_btn.configure(text="Lock" if self.unlocked else "Unlock")
        self.msg.set("Unlocked." if self.unlocked
                     else "Locked. Unlock to change settings.")

    def _require_unlock(self):
        if not self.unlocked:
            messagebox.showwarning("Locked", "Unlock first.")
            return False
        return True

    # ---- command channel ----
    def _password_for_cmd(self):
        return simpledialog.askstring("Confirm", "Password:", show="*",
                                      parent=self.root)

    def send_command(self, cmd):
        if not self._require_unlock():
            return
        pw = self._password_for_cmd()
        if pw is None:
            return
        if _hash(pw, self.cfg["salt"]) != self.cfg["pw_hash"]:
            messagebox.showerror("Command", "Wrong password.")
            return
        if os.path.exists(COMMAND_PATH):
            if not messagebox.askyesno(
                    "Command pending",
                    "A previous command has not been consumed by the backend "
                    "(it may be down). Overwrite it?"):
                return
        cid = int(time.time() * 1000)
        try:
            _atomic_write(COMMAND_PATH, f"id={cid}\nauth={pw}\ncmd={cmd}\n")
            self.last_cmd_id = cid
            self.msg.set(f"Sent: {cmd}")
        except OSError as e:
            messagebox.showerror("Command", f"Could not write command: {e}")

    def do_zero(self):
        self.send_command("ZERO")

    def do_set_gain(self):
        self.send_command(f"SET_GAIN {self.gain_var.get()}")

    def do_autorange(self):
        on = self.auto_btn.cget("text").endswith("ON")
        self.send_command("AUTORANGE " + ("ON" if on else "OFF"))
        self.auto_btn.configure(text="Autorange OFF" if on else "Autorange ON")

    def do_set_alarms(self):
        if not self._require_unlock():
            return
        self.cfg["alarm_high_fa"] = self.alarm_high.get().strip()
        self.cfg["alarm_low_fa"] = self.alarm_low.get().strip()
        save_config(self.cfg)
        # Also inform the backend (best effort); frontend evaluates locally too.
        hi = self.cfg["alarm_high_fa"]
        lo = self.cfg["alarm_low_fa"]
        if hi:
            self.send_command(f"SET_ALARM_HIGH {hi}")
        if lo:
            self.send_command(f"SET_ALARM_LOW {lo}")
        self.msg.set("Alarm levels saved.")

    # ---- temperature correction (4) ----
    def do_temp_toggle(self):
        if not self._require_unlock():
            self.temp_correct.set(self.cfg.get("temp_correct", False))
            return
        self.cfg["temp_correct"] = self.temp_correct.get()
        save_config(self.cfg)

    def do_set_tref(self):
        if not self._require_unlock():
            return
        d, _ = read_status()
        t = self._to_float(d.get("cpu_temp_c")) if d else None
        if t is None:
            messagebox.showwarning("T_ref", "No temperature in status yet.")
            return
        self.cfg["t_ref_c"] = t
        save_config(self.cfg)
        self.tref_var.set(self._tref_text())

    def _tref_text(self):
        tr = self.cfg.get("t_ref_c", "")
        d = self.cfg.get("drift_pct_per_c", DEFAULT_DRIFT_PCT_PER_C)
        return f"(T_ref {tr} °C, {d} %/°C)" if tr != "" else "(T_ref not set)"

    def _temp_correct(self, fa, temp_c):
        """Undo amplifier gain drift vs temperature, using the configured drift
        rate. Validate before trusting."""
        if not self.cfg.get("temp_correct"):
            return None
        tr = self.cfg.get("t_ref_c", "")
        if fa is None or temp_c is None or tr == "":
            return None
        drift = self.cfg.get("drift_pct_per_c", DEFAULT_DRIFT_PCT_PER_C) / 100.0
        factor = 1.0 + drift * (temp_c - float(tr))
        return fa / factor if factor != 0 else None

    # ---- poll loop ----
    @staticmethod
    def _to_float(s):
        try:
            return float(s)
        except (TypeError, ValueError):
            return None

    def poll(self):
        d, age = read_status()
        stale = (d is None) or (age is None) or (age > STALE_MS)

        fa = self._to_float(d.get("current_fA")) if d else None
        temp = self._to_float(d.get("cpu_temp_c")) if d else None

        self.current_var.set(format_current(fa))

        corrected = self._temp_correct(fa, temp)
        self.corrected_var.set(
            f"temp-corrected: {format_current(corrected)}" if corrected is not None else "")

        if d:
            self.field["state"].set(d.get("state", "--"))
            self.field["stability"].set(d.get("stability", "--"))
            ge = d.get("gain_exp", "")
            self.field["gain"].set(f"10^{ge}" if ge else "--")
            self.field["temp"].set(f"{temp:.1f} °C" if temp is not None else "--")
            self.field["overload"].set("YES" if d.get("overload") == "1" else "no")
        else:
            for k in ("state", "stability", "gain", "temp", "overload"):
                self.field[k].set("--")

        # connection / staleness
        if d is None:
            self.field["conn"].set("no status file (backend not running?)")
        elif stale:
            self.field["conn"].set(f"STALE ({age/1000:.1f}s) - {d.get('backend','?')}")
        else:
            self.field["conn"].set(d.get("backend", "?"))

        # alarm (5): frontend-side evaluation so it works even if backend down
        alarm = self._eval_alarm(fa, d, stale)
        self.field["alarm"].set(alarm)
        self._color_for_alarm(alarm, stale)

        self.root.after(POLL_MS, self.poll)

    def _eval_alarm(self, fa, d, stale):
        if stale or fa is None:
            return "-- (no data)"
        hi = self._to_float(self.cfg.get("alarm_high_fa"))
        lo = self._to_float(self.cfg.get("alarm_low_fa"))
        if hi is not None and fa > hi:
            return f"HIGH (> {hi} fA)"
        if lo is not None and fa < lo:
            return f"LOW (< {lo} fA)"
        # fall back to backend's own flag if it set one
        if d and d.get("alarm") in ("HIGH", "LOW"):
            return d["alarm"]
        return "OK"

    def _color_for_alarm(self, alarm, stale):
        if stale:
            color = "gray"
        elif alarm.startswith(("HIGH", "LOW")):
            color = "red"
            self.root.bell()
        elif alarm == "OK":
            color = "green"
        else:
            color = "black"
        self.current_lbl.configure(fg=color)


def main():
    import simulator

    if "--selftest" in sys.argv:
        simulator.atomic_write(STATUS_PATH,
            "schema=1\nepoch_ms=%d\nbackend=RUNNING\nstate=MEASURE\ngain_exp=8\n"
            "current_fA=42.5\nmean_V=4.25e-6\nstability=STABLE\noverload=0\n"
            "cpu_temp_c=40.2\nalarm=OK\n" % int(time.time() * 1000))
        root = tk.Tk()
        app = Frontend(root)
        for _ in range(3):
            root.update()
        print("selftest OK: current=%s" % app.current_var.get())
        root.destroy()
        return 0

    if "--screenshot" in sys.argv:
        import time as _t
        from PIL import ImageGrab
        path = sys.argv[sys.argv.index("--screenshot") + 1]
        # Representative MEASURE snapshot so the panel shows live values.
        simulator.atomic_write(STATUS_PATH,
            "schema=1\nepoch_ms=%d\nbackend=RUNNING\nstate=MEASURE\ngain_exp=8\n"
            "current_fA=43.87\nmean_V=4.387e-6\nstability=STABLE\noverload=0\n"
            "cpu_temp_c=40.3\nalarm=OK\n" % int(time.time() * 1000))
        root = tk.Tk()
        root.geometry("+80+80")
        Frontend(root)
        root.attributes("-topmost", True)
        root.lift()
        root.focus_force()
        for _ in range(6):
            root.update()
            _t.sleep(0.05)
        root.update_idletasks()
        x, y = root.winfo_rootx(), root.winfo_rooty()
        w, h = root.winfo_width(), root.winfo_height()
        ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(path)
        print(f"saved screenshot to {path}")
        root.destroy()
        return 0

    # Normal run: start the simulator in the background, then show the panel.
    simulator.start_in_thread()
    root = tk.Tk()
    Frontend(root)
    root.mainloop()


if __name__ == "__main__":
    main()
