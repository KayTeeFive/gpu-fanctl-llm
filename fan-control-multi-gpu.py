#!/usr/bin/env python3
"""
GPU Fan Controller — multi-GPU, systemd-ready
Supports: WX9100 / MI25 (configurable via PCI_ID_TARGET)
"""

import glob
import logging
import os
import socket
import sys
import threading
import time
import atexit
from datetime import datetime, timedelta

# ============================================================
# CONFIG
# ============================================================

# --- Target GPU ---
TARGET_NAME    = "WX9100"
PCI_ID_TARGET  = "0x1002:0x6861"
# TARGET_NAME  = "MI25"
# PCI_ID_TARGET = "0x1002:0x6860"

# --- Fan limits ---
MAX_RPM = 4000

# --- Power override ---
POWER_LIMIT            = 155   # W
POWER_SAMPLES_TRIGGER  = 3     # consecutive samples above limit

# --- Hysteresis / timing ---
TEMP_HYST      = 2          # °C — minimum change to consider temp "changed"
DOWN_DELAY     = 5          # s  — minimum time between fan speed reductions
RPM_TOLERANCE  = 100        # RPM — drift threshold before forced correction
PERCENT_TOLERANCE  = 3      # % — drift threshold before forced correction
FORCE_INTERVAL = 10         # s  — heartbeat: force RPM write even without temp change

# --- Watchdog / systemd ---
WATCHDOG_TIMEOUT  = 20      # s — restart GPU thread if silent for this long
SYSTEMD_INTERVAL  = 10      # s — how often to ping systemd watchdog

# --- Poll interval ---
POLL_INTERVAL = 1           # s

# --- ANSI colours ---
RESET     = "\033[0m"
GREEN     = "\033[32m"
YELLOW    = "\033[33m"
BOLD_BLUE = "\033[1;34m"
RED       = "\033[31m"

# Per-GPU label colours (cyan, magenta, then fallbacks)
GPU_COLORS = [
    "\033[1;36m",  # Bold Cyan    — GPU 0
    "\033[1;35m",  # Bold Magenta — GPU 1
    "\033[1;33m",  # Bold Yellow  — GPU 2
    "\033[1;32m",  # Bold Green   — GPU 3
]

# --- % → RPM table (WX9100) ---
PERCENT_TO_RPM = {
    10: 1165, 15: 1330, 20: 1480, 25: 1630,
    30: 1790, 35: 1960, 40: 2125, 45: 2270,
    50: 2440, 55: 2605, 60: 2780, 65: 2920,
    70: 3100, 75: 3260, 80: 3440, 85: 3575,
    90: 3750, 95: 3920, 100: 4000,
}

# --- Fan curves: (temp °C, fan %) ---
CURVES = {
    "edge": [
        (30, 10),
        (40, 15),
        (50, 20),
        (60, 50),
        (65, 75),
        (70, 90),
        (75, 100),
        (80, 100),  # crit - 5
        (85, 100),  # critical
        (90, 100),  # emergency
    ],
    "junction": [
        (30, 10),
        (40, 15),
        (50, 20),
        (65, 40),
        (75, 60),
        (85, 80),
        (90, 95),
        (100, 100), # crit - 5
        (105, 100), # critical
        (110, 100), # emergency
    ],
    "mem": [
        (30, 10),
        (40, 15),
        (50, 20),
        (65, 65),
        (75, 75),
        (85, 85),
        (88, 90),
        (90, 100),  # crit - 5
        (95, 100),  # critical
        (100, 100), # emergency
    ]
}


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    #format="%(asctime)s [%(levelname)s] %(message)s",
    format="[%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

log = logging.getLogger("fan-control")


# ============================================================
# HARDWARE DISCOVERY
# ============================================================

def find_gpus_hwmon() -> list[str]:
    """Return sorted list of hwmon sysfs paths for all matching GPUs."""
    hwmons = []
    for card_path in sorted(glob.glob("/sys/class/drm/card*/device")):
        try:
            with open(os.path.join(card_path, "vendor")) as f:
                vendor = f.read().strip()
            with open(os.path.join(card_path, "device")) as f:
                device = f.read().strip()
        except FileNotFoundError:
            continue
        if f"{vendor}:{device}".lower() == PCI_ID_TARGET.lower():
            for hw in glob.glob(os.path.join(card_path, "hwmon", "hwmon*")):
                hwmons.append(hw)
    return sorted(hwmons)


# ============================================================
# SYSFS HELPERS
# ============================================================

def read_int(path: str) -> int:
    with open(path) as f:
        return int(f.read())


def read_metrics(hw: str) -> dict:
    return {
        "edge":     read_int(f"{hw}/temp1_input") / 1000.0,
        "junction": read_int(f"{hw}/temp2_input") / 1000.0,
        "mem":      read_int(f"{hw}/temp3_input") / 1000.0,
        "power":    read_int(f"{hw}/power1_input") / 1_000_000,
    }


def get_current_rpm(hw: str) -> int | None:
    try:
        return read_int(f"{hw}/fan1_target")
    except Exception:
        return 3500


def get_current_percent(hw: str) -> float | None:
    try:
        current_pwm = read_int(f"{hw}/pwm1")
        return 100 * current_pwm / 255
    except Exception:
        return 100


def ensure_manual(hw: str) -> None:
    try:
        if read_int(f"{hw}/pwm1_enable") != 1:
            with open(f"{hw}/pwm1_enable", "w") as f:
                f.write("1")
            log.warning(f"[WARN] Re-enabled manual mode on {hw}")
    except Exception:
        pass


def set_speed(_hw: str, _target_percent: int, _target_rpm: int) -> None:
    try:
        pwm = int(_target_percent * 255 / 100)
        with open(f"{_hw}/pwm1", "w") as f:
            f.write(str(pwm))
    except Exception:
        with open(f"{_hw}/fan1_target", "w") as f:
            f.write(str(int(_target_rpm)))


def restore_auto(hw: str) -> None:
    try:
        with open(f"{hw}/pwm1_enable", "w") as f:
            f.write("2")
    except Exception:
        pass


# ============================================================
# FAN CURVE MATH
# ============================================================

def interpolate(temp: float, curve: list) -> float:
    if temp <= curve[0][0]:
        return curve[0][1]
    if temp >= curve[-1][0]:
        return curve[-1][1]
    for i in range(len(curve) - 1):
        t1, p1 = curve[i]
        t2, p2 = curve[i + 1]
        if t1 <= temp <= t2:
            k = (temp - t1) / (t2 - t1)
            return p1 + k * (p2 - p1)
    return curve[-1][1]


def percent_to_rpm(percent: float) -> int:
    percent = int(round(percent / 5) * 5)
    percent = max(10, min(100, percent))
    return PERCENT_TO_RPM[percent]


# ============================================================
# SYSTEMD NOTIFY
# ============================================================

def _sd_send(msg: bytes) -> None:
    notify_socket = os.getenv("NOTIFY_SOCKET")
    if not notify_socket:
        return
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(notify_socket)
            s.sendall(msg)
    except Exception as e:
        log.warning(f"[WARN] systemd notify failed: {e}")


def sd_notify_ready()    -> None: _sd_send(b"READY=1")
def sd_notify_watchdog() -> None: _sd_send(b"WATCHDOG=1")


# ============================================================
# PER-GPU WORKER
# ============================================================

def gpu_worker(hw: str, stop_event: threading.Event, alive_cb, color: str = "") -> None:
    """
    Control loop for a single GPU hwmon path.
    Calls alive_cb() every iteration so the watchdog knows the thread is alive.
    """
    label = f"{color}{os.path.basename(hw)}{RESET}"

    last_percent:       float | None = None
    last_temp_snapshot: list  | None = None
    last_down_time:     float        = 0.0
    last_force_time:    float        = 0.0
    power_hits:         int          = 0

    while not stop_event.is_set():
        try:
            m = read_metrics(hw)
        except Exception as e:
            log.error(f"[{label}] read error: {e}")
            time.sleep(POLL_INTERVAL)
            alive_cb()
            continue

        # ── Power override ───────────────────────────────────
        if m["power"] >= POWER_LIMIT:
            power_hits += 1
        else:
            power_hits = 0
        power_override = power_hits >= POWER_SAMPLES_TRIGGER

        # ── Target fan % from curves ─────────────────────────
        target_percent = max(
            interpolate(m["edge"],     CURVES["edge"]),
            interpolate(m["junction"], CURVES["junction"]),
            interpolate(m["mem"],      CURVES["mem"]),
        )

        if power_override:
            target_percent = max(target_percent, 75)
            log.info(
                f"[{label}] {YELLOW}Power override: "
                f"{m['power']:.1f}W > {POWER_LIMIT}W → {target_percent}%{RESET}"
            )

        # ── Colour temp display ──────────────────────────────
        temps   = {"edge": m["edge"], "junction": m["junction"], "mem": m["mem"]}
        hottest = max(temps, key=temps.get)
        coldest = min(temps, key=temps.get)
        log.info(
            f"[{label}] " +
            " | ".join(
                f"{s.upper()}: "
                f"{RED if s == hottest else GREEN if s == coldest else YELLOW}"
                f"{temps[s]}°C{RESET}"
                for s in ["edge", "junction", "mem"]
            )
        )

        # ── Hysteresis logic ─────────────────────────────────
        snap = (round(m["edge"]), round(m["junction"]), round(m["mem"]))
        temp_changed = (
            last_temp_snapshot is None or
            any(abs(snap[i] - last_temp_snapshot[i]) >= TEMP_HYST for i in range(3))
        )

        now = time.monotonic()

        if last_percent is None:
            apply = True
        elif target_percent > last_percent:
            apply = True
        elif target_percent < last_percent:
            apply = temp_changed and (now - last_down_time >= DOWN_DELAY)
            if apply:
                last_down_time = now
        else:
            apply = False

        # ── Heartbeat force ──────────────────────────────────
        if now - last_force_time >= FORCE_INTERVAL:
            apply = True
            last_force_time = now

        # ── Apply + drift correction ─────────────────────────
        target_rpm     = percent_to_rpm(target_percent)
        current_rpm = get_current_rpm(hw)
        current_percent = get_current_percent(hw)
        drift = abs(current_percent - target_percent)
        #is_drift   = current_rpm is None or abs(current_rpm - target_rpm) > RPM_TOLERANCE
        is_drift   = current_percent is None or abs(current_percent - target_percent) > PERCENT_TOLERANCE

        log.info(f"[{label}] Target: {target_percent:.1f}% / {target_rpm} rpm | Current: {current_percent:.1f}% / {current_rpm} rpm | Drift: {drift:.1f}%")

        ensure_manual(hw)
        if apply or is_drift:
            if is_drift and not apply:
                log.info(f"[{label}] {YELLOW}[DRIFT] {current_percent:.1f}% → {target_percent:.1f}% {RESET}")
            set_speed(hw, target_percent, target_rpm)

        if apply:
            last_percent       = target_percent
            last_temp_snapshot = list(snap)
            log.info(
                f"[{label}] {BOLD_BLUE}SET {target_percent:.1f}% → {target_rpm} RPM"
                f" | power={m['power']:.1f}W | temps={snap}{RESET}"
            )

        alive_cb()
        time.sleep(POLL_INTERVAL)

    log.info(f"[{label}] worker stopped")


# ============================================================
# WATCHDOG / MANAGER
# ============================================================

class GpuManager:
    def __init__(self, hwmons: list[str]):
        self.hwmons       = hwmons
        self._lock        = threading.Lock()
        self._last_seen:  dict[str, datetime]       = {}
        self._threads:    dict[str, threading.Thread] = {}
        self._stops:      dict[str, threading.Event] = {}
        self._gpu_colors: dict[str, str] = {
            hw: GPU_COLORS[i % len(GPU_COLORS)]
            for i, hw in enumerate(hwmons)
        }

    # ── alive callback called by each worker ────────────────
    def _alive(self, hw: str) -> None:
        with self._lock:
            self._last_seen[hw] = datetime.now()

    def _start_worker(self, hw: str) -> None:
        stop = threading.Event()
        t = threading.Thread(
            target=gpu_worker,
            args=(hw, stop, lambda: self._alive(hw), self._gpu_colors.get(hw, "")),
            daemon=True,
            name=f"gpu-{os.path.basename(hw)}",
        )
        self._stops[hw]   = stop
        self._threads[hw] = t
        with self._lock:
            self._last_seen[hw] = datetime.now()
        t.start()
        log.info(f"[manager] started worker for {hw}")

    def run(self) -> None:
        # initial workers
        for hw in self.hwmons:
            self._start_worker(hw)

        sd_notify_ready()
        last_sd_ping = datetime.now()

        while True:
            now = datetime.now()

            # ── restart dead workers ─────────────────────────
            with self._lock:
                snapshot = dict(self._last_seen)

            for hw, last_time in snapshot.items():
                if now - last_time > timedelta(seconds=WATCHDOG_TIMEOUT):
                    log.info(
                        f"[manager] {YELLOW}[WATCHDOG] {hw} unresponsive "
                        f"for >{WATCHDOG_TIMEOUT}s — restarting{RESET}"
                    )
                    self._stops[hw].set()           # signal old thread
                    self._start_worker(hw)          # launch fresh thread

            # ── systemd watchdog ping ────────────────────────
            if (now - last_sd_ping).total_seconds() >= SYSTEMD_INTERVAL:
                sd_notify_watchdog()
                last_sd_ping = now

            time.sleep(1)


# ============================================================
# CLEANUP
# ============================================================

def cleanup() -> None:
    for hw in find_gpus_hwmon():
        restore_auto(hw)
    log.info(f"{YELLOW}Restored AUTO fan control on all GPUs{RESET}")

atexit.register(cleanup)


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    hwmons = find_gpus_hwmon()
    if not hwmons:
        log.warning(f"No {TARGET_NAME} GPUs found (PCI ID {PCI_ID_TARGET}). Exiting.")
        raise SystemExit(1)

    log.info(f"Found {len(hwmons)} × {TARGET_NAME}:")
    for hw in hwmons:
        log.info(f"  {hw}")

    GpuManager(hwmons).run()
