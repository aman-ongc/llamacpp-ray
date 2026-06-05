#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Update Windows netsh portproxy rules to forward LLM platform ports to WSL2.

.DESCRIPTION
    WSL2 gets a new virtual IP on every reboot. This script reads the current
    WSL2 IP and refreshes all portproxy rules so Windows browser / external
    clients can reach services running inside WSL2.

    Run after every reboot, or call from start_windows.ps1 automatically.

.EXAMPLE
    # Run standalone (elevated PowerShell):
    .\update_portproxy.ps1

    # Specify a different distro:
    .\update_portproxy.ps1 -WslDistro "Ubuntu-22.04"
#>

param(
    [string]$WslDistro = "Ubuntu-24.04"
)

$ErrorActionPreference = "Stop"

function Write-Info { param([string]$Msg) Write-Host "[INFO]  $Msg" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Msg) Write-Host "[OK]    $Msg" -ForegroundColor Green }
function Write-Warn { param([string]$Msg) Write-Host "[WARN]  $Msg" -ForegroundColor Yellow }

# Ports to forward: connectport = listenport (same port both sides)
$Ports = @(
    @{ Port = 10080; Label = "NGINX / API"     },
    @{ Port = 13000; Label = "Grafana"          },
    @{ Port = 18000; Label = "FastAPI Gateway"  },
    @{ Port = 9090;  Label = "Prometheus"       },
    @{ Port = 8265;  Label = "Ray Dashboard"    }
)

# ── Get current WSL2 IP ───────────────────────────────────────────────────────
Write-Info "Resolving WSL2 IP for distro '$WslDistro'..."
try {
    $wslIp = (wsl.exe -d $WslDistro -e bash -c "hostname -I | awk '{print `$1}'").Trim()
} catch {
    Write-Warning "wsl.exe failed. Is WSL2 running?"
    exit 1
}

if ([string]::IsNullOrWhiteSpace($wslIp) -or $wslIp -notmatch '^\d+\.\d+\.\d+\.\d+$') {
    Write-Warning "Could not get a valid WSL2 IP (got: '$wslIp'). Is WSL2 started?"
    exit 1
}

Write-Ok "WSL2 IP: $wslIp"

# ── Refresh portproxy rules ───────────────────────────────────────────────────
Write-Info "Updating netsh portproxy rules..."
foreach ($entry in $Ports) {
    $p = $entry.Port
    $label = $entry.Label

    # Delete any existing rule for this port (ignore error if absent)
    netsh interface portproxy delete v4tov4 `
        listenport=$p listenaddress=0.0.0.0 2>$null | Out-Null

    # Add fresh rule pointing to current WSL2 IP
    netsh interface portproxy add v4tov4 `
        listenport=$p listenaddress=0.0.0.0 `
        connectport=$p connectaddress=$wslIp | Out-Null

    Write-Ok "  0.0.0.0:$p  ->  ${wslIp}:$p  ($label)"
}

# ── Ensure firewall allows inbound on these ports ────────────────────────────
Write-Info "Refreshing firewall rule 'WSL2-LLM-Ports'..."
$portList = ($Ports | ForEach-Object { $_.Port }) -join ","

netsh advfirewall firewall delete rule name="WSL2-LLM-Ports" 2>$null | Out-Null
netsh advfirewall firewall add rule `
    name="WSL2-LLM-Ports" `
    dir=in `
    action=allow `
    protocol=TCP `
    localport=$portList | Out-Null

Write-Ok "Firewall rule updated for ports: $portList"

# ── Show current rules ────────────────────────────────────────────────────────
Write-Host ""
Write-Info "Current portproxy table:"
netsh interface portproxy show v4tov4