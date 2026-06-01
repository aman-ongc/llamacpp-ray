#!/usr/bin/env python3
"""
Enable OpenSSH Server on Windows worker nodes via WMI execution.
Run this on the controller node (10.208.211.62) inside the llm-platform venv.
"""
import os
import subprocess
import sys

WORKERS = ["10.208.211.54", "10.208.211.59"]
USERNAME = "administrator"
PASSWORD = "Ongc@1234"
DOMAIN = "."

PS_CMD = (
    'powershell -Command "'
    "if ((Get-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0).State -ne 'Installed') {"
    "  Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0"
    "}; "
    "Set-Service -Name sshd -StartupType Automatic; "
    "Start-Service sshd; "
    "netsh advfirewall firewall add rule name='OpenSSH-In' protocol=TCP dir=in localport=22 action=allow | Out-Null; "
    'Write-Output \\"SSH_ENABLED\\"'
    '"'
)

WMIEXEC = os.path.expanduser("~/.local/bin/wmiexec.py")

env = os.environ.copy()
env["no_proxy"] = "10.0.0.0/8,localhost,127.0.0.1"
env["NO_PROXY"] = "10.0.0.0/8,localhost,127.0.0.1"

for worker in WORKERS:
    print(f"\n=== Enabling SSH on {worker} ===", flush=True)
    cmd = [
        sys.executable,
        WMIEXEC,
        f"{DOMAIN}/{USERNAME}:{PASSWORD}@{worker}",
        PS_CMD,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        print("STDOUT:", result.stdout[:800])
        if result.stderr:
            print("STDERR:", result.stderr[:400])
        print("RC:", result.returncode)
    except subprocess.TimeoutExpired:
        print("TIMEOUT after 120s")
    except Exception as e:
        print(f"ERROR: {e}")
