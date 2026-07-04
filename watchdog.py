"""
Daemon watchdog — keeps the trading daemon alive.

Checks every 60 seconds whether the daemon is running.
If it died, restarts it automatically and logs the event.

Usage:
  pythonw watchdog.py    (background)
  python watchdog.py     (foreground, with output)

This itself can be installed in Windows Startup folder so the system
self-recovers from reboots, daemon crashes, transient errors, etc.
"""

import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

# Windows flag to hide the console window that pops up for each subprocess call
CREATE_NO_WINDOW = 0x08000000

PROJECT_DIR = Path(__file__).resolve().parent
LOG_FILE = PROJECT_DIR / "trading_system" / "output" / "watchdog.log"
DAEMON_LOG = PROJECT_DIR / "trading_system" / "output" / "daemon.log"
CHECK_INTERVAL_SEC = 60
DAEMON_CMD = ["pythonw", str(PROJECT_DIR / "run.py"), "auto", "start", "--live"]


def log(msg: str):
    """Append to watchdog log."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}\n"
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)
    print(line.strip())


def is_daemon_running() -> bool:
    """
    Check if the trading daemon (run.py auto start) is running.
    Uses PowerShell Get-CimInstance to read process command lines reliably.
    """
    try:
        ps_cmd = (
            "Get-CimInstance Win32_Process -Filter \"name='pythonw.exe'\" | "
            "Select-Object -ExpandProperty CommandLine"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=15,
            creationflags=CREATE_NO_WINDOW,  # hide console window
        )
        for line in result.stdout.splitlines():
            if "run.py" in line and "auto" in line and "start" in line:
                return True
        return False
    except Exception as e:
        log(f"is_daemon_running check failed: {e}")
        # On check failure, assume daemon IS running to avoid restart loop
        return True


def start_daemon():
    """Launch the daemon in the background."""
    try:
        # Open daemon.log for writing (overwrite)
        log_handle = open(DAEMON_LOG, "w", encoding="utf-8")
        # Use DETACHED_PROCESS flag (0x00000008) so daemon survives watchdog death
        # CREATE_NEW_PROCESS_GROUP (0x00000200) so Ctrl+C doesn't propagate
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200

        subprocess.Popen(
            DAEMON_CMD,
            cwd=PROJECT_DIR,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
        log("Daemon started.")
    except Exception as e:
        log(f"Failed to start daemon: {e}")


def main():
    log("=== Watchdog started ===")
    log(f"Project dir: {PROJECT_DIR}")
    log(f"Check interval: {CHECK_INTERVAL_SEC}s")

    # Initial start if not already running
    if not is_daemon_running():
        log("Daemon not running on watchdog start — launching.")
        start_daemon()
        time.sleep(5)

    # Main loop
    while True:
        try:
            time.sleep(CHECK_INTERVAL_SEC)
            if not is_daemon_running():
                log("Daemon DOWN — restarting.")
                start_daemon()
                time.sleep(10)  # give it time to come up before next check
        except KeyboardInterrupt:
            log("Watchdog stopped by user.")
            break
        except Exception as e:
            log(f"Watchdog loop error: {e}")
            time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
