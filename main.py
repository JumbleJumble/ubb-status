#!/usr/bin/env python3
import math
import os
import subprocess
import time

HOSTS = ["192.168.4.37", "192.168.4.38"]
SSH_USER = "jumble.jumble"
SSH_KEY = os.path.expanduser(os.environ.get("SSH_KEY_PATH", "/run/secrets/ubb_key"))
PERIOD = 40.0
RATE = 0.5
LEDBAR = "/proc/ubnt_ledbar"

SSH_OPTS = [
    "-i", SSH_KEY,
    "-o", "StrictHostKeyChecking=no",
    "-o", "HostKeyAlgorithms=+ssh-rsa",
    "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
]


def color_at_phase(phase: float) -> tuple[int, int, int]:
    # Sinusoidal interpolation along ff0000 -> ff00ff -> 0000ff path
    u = (1 - math.cos(phase)) / 2
    if u <= 0.5:
        r, g, b = 255, 0, int(255 * u * 2)
    else:
        r, g, b = int(255 * (1 - u) * 2), 0, 255
    return r, g, b


def open_shell(host: str) -> subprocess.Popen:
    proc = subprocess.Popen(
        ["ssh", *SSH_OPTS, f"{SSH_USER}@{host}", "/bin/sh"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"Connected to {host}")
    return proc


def set_color(proc: subprocess.Popen, r: int, g: int, b: int) -> None:
    proc.stdin.write(f"echo '{r},{g},{b}' > {LEDBAR}/color\n".encode())
    proc.stdin.flush()


def run() -> None:
    print("Connecting to UBBs...")
    shells = [open_shell(h) for h in HOSTS]

    print("Starting gradient loop.")
    tick = 0
    start = time.monotonic()

    while True:
        elapsed = tick / RATE
        base_phase = (2 * math.pi * elapsed / PERIOD) % (2 * math.pi)

        phases = [base_phase, (base_phase + math.pi) % (2 * math.pi)]
        for proc, phase in zip(shells, phases):
            r, g, b = color_at_phase(phase)
            set_color(proc, r, g, b)

        tick += 1
        sleep_time = (start + tick / RATE) - time.monotonic()
        if sleep_time > 0:
            time.sleep(sleep_time)


if __name__ == "__main__":
    run()
