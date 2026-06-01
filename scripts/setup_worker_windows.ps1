# setup_worker_windows.ps1
# Run as Administrator on each Windows worker node (WS-03, WS-08, WS-13).
# Enables OpenSSH Server, starts it, and configures WSL2 port forwarding
# so that SSH to the Windows IP reaches the WSL2 shell.
#
# Usage (from an elevated PowerShell prompt):
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\setup_worker_windows.ps1 -WslDistro "Ubuntu-24.04"

param(
    [string]$WslDistro = "Ubuntu-24.04",
    [int]$SshPort = 22
)

$ErrorActionPreference = "Stop"

Write-Host "=== Worker Setup: OpenSSH + WSL2 Port Forwarding ===" -ForegroundColor Cyan

# 1. Install OpenSSH Server capability if not present.
$cap = Get-WindowsCapability -Online -Name "OpenSSH.Server~~~~0.0.1.0"
if ($cap.State -ne "Installed") {
    Write-Host "Installing OpenSSH Server..."
    Add-WindowsCapability -Online -Name "OpenSSH.Server~~~~0.0.1.0"
} else {
    Write-Host "OpenSSH Server already installed."
}

# 2. Start and auto-start sshd.
Set-Service -Name sshd -StartupType Automatic
Start-Service sshd -ErrorAction SilentlyContinue
Write-Host "sshd service started."

# 3. Allow SSH through Windows Firewall.
$rule = Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue
if (-not $rule) {
    New-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -DisplayName "OpenSSH Server (sshd)" `
        -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort $SshPort
    Write-Host "Firewall rule added for port $SshPort."
} else {
    Write-Host "Firewall rule already exists."
}

# 4. Get WSL2 internal IP.
$wslIp = (wsl.exe -d $WslDistro -- hostname -I).Trim().Split()[0]
Write-Host "WSL2 IP: $wslIp"

# 5. Forward Windows port 22 to WSL2 SSH.
#    WSL2 runs its own sshd; forward traffic from the Windows NIC to it.
$existing = netsh interface portproxy show v4tov4 | Select-String ":$SshPort"
if ($existing) {
    netsh interface portproxy delete v4tov4 listenport=$SshPort listenaddress=0.0.0.0 | Out-Null
}
netsh interface portproxy add v4tov4 listenport=$SshPort listenaddress=0.0.0.0 `
    connectport=$SshPort connectaddress=$wslIp
Write-Host "Port proxy: 0.0.0.0:$SshPort -> $wslIp:$SshPort"

# 6. Ensure sshd is running inside WSL2.
wsl.exe -d $WslDistro -- bash -c "sudo service ssh start 2>/dev/null || sudo systemctl start ssh 2>/dev/null || true"
Write-Host "WSL2 sshd started."

# 7. Verify.
$svc = Get-Service -Name sshd
Write-Host "Windows sshd status: $($svc.Status)"
Write-Host ""
Write-Host "=== Setup complete ===" -ForegroundColor Green
Write-Host "Test from controller:  ssh administrator@<this-node-ip>"
