#Requires -Version 5.1

param(
    [string]$WslDistro = "Ubuntu-24.04",
    [int[]]$Ports = @(9090, 13000, 8265, 18000, 10080, 8001, 8080, 15432, 16379)
)

$ErrorActionPreference = "Stop"

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin) {
    throw "Run this script from an elevated PowerShell session."
}

$wslIp = (wsl.exe -d $WslDistro -e bash -lc "hostname -I").Trim().Split()[0]
if (-not $wslIp) {
    throw "Could not resolve WSL IP for $WslDistro"
}

foreach ($port in $Ports) {
    netsh interface portproxy delete v4tov4 listenaddress=127.0.0.1 listenport=$port 2>$null | Out-Null
    netsh interface portproxy add v4tov4 `
        listenaddress=127.0.0.1 listenport=$port `
        connectaddress=$wslIp connectport=$port | Out-Null

    $ruleName = "ONGC LLM localhost $port"
    if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
            -Protocol TCP -LocalAddress 127.0.0.1 -LocalPort $port | Out-Null
    }
}

netsh interface portproxy show all
