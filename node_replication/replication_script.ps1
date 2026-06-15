#Requires -RunAsAdministrator

<#
.SYNOPSIS
    ONGC LLM Node — Base Environment Setup
    Gets a fresh Windows workstation to the same WSL2 / NVIDIA / CUDA / Docker
    state as WS-11.  LLM stack (llama.cpp, venvs, project) handled separately.

.DESCRIPTION
    Phases (one Windows reboot, the rest run sequentially after):
      0  Enable WSL2 + VirtualMachinePlatform features → schedule task → reboot
      1  WSL kernel update, Ubuntu 24.04 install, user creation,
         /etc/wsl.conf, .wslconfig (mirrored networking) → wsl --shutdown
         → verify hostname -I returns 10.* (hard gate)
      2  Proxy config: /etc/environment + /etc/apt/apt.conf.d/99proxy
      3  apt base packages + CUDA Toolkit 12.8 (WSL repo) + Docker + SSH
      4  Verification: nvidia-smi, nvcc, docker, ssh, mirrored IP

.USAGE
    # First run — from elevated PowerShell:
    powershell -ExecutionPolicy Bypass -File replication_script.ps1 `
        -NodeIP 10.208.211.XX -NodeHostname PVLPOCC00WS0XX

    # Resume after failure (reads state file, continues from last phase):
    powershell -ExecutionPolicy Bypass -File replication_script.ps1

    # Reset and start over:
    powershell -ExecutionPolicy Bypass -File replication_script.ps1 -Reset

.PREREQUISITES (manual, before running)
    - NVIDIA driver >= 595.79 installed on Windows
    - Windows 11 build >= 10.0.22621  (required for mirrored networking)
    - Run from an elevated (Administrator) PowerShell session
#>

param(
    [switch]$Reset,
    [string]$NodeIP       = "",
    [string]$NodeHostname = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
$SetupDir   = "C:\ProgramData\WSLNodeSetup"
$StateFile  = "$SetupDir\state.json"
$LogFile    = "$SetupDir\setup.log"
$TaskName   = "WSLNodeSetupContinue"
$ScriptPath = $MyInvocation.MyCommand.Path

$WslDistro   = "Ubuntu-24.04"
$WslUser     = "administrator"
$WslPassword = "Ongc@1234"

$ProxyUrl  = "http://10.205.122.201:8080"
$NoProxy   = "localhost,127.0.0.1,10.208.211.0/24"

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
function Log {
    param([string]$Msg, [string]$Level = "INFO")
    $ts    = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line  = "[$ts][$Level] $Msg"
    $color = switch ($Level) {
        "ERROR" { "Red" }; "WARN" { "Yellow" }; "OK" { "Green" }
        default { "Cyan" }
    }
    Write-Host $line -ForegroundColor $color
    try { Add-Content -Path $LogFile -Value $line } catch {}
}

function Die  { param([string]$M); Log $M "ERROR"; exit 1 }

function Step {
    param([string]$M)
    Write-Host ""
    Write-Host ("─" * 60) -ForegroundColor Magenta
    Write-Host "  $M" -ForegroundColor Magenta
    Write-Host ("─" * 60) -ForegroundColor Magenta
    Log $M
}

# ─────────────────────────────────────────────────────────────────────────────
# State
# ─────────────────────────────────────────────────────────────────────────────
function Get-State {
    if (Test-Path $StateFile) {
        return Get-Content $StateFile -Raw | ConvertFrom-Json
    }
    return [PSCustomObject]@{ Phase = 0; NodeIP = ""; NodeHostname = "" }
}

function Save-State {
    param([PSCustomObject]$s)
    $s | ConvertTo-Json | Set-Content -Path $StateFile -Encoding UTF8
}

# ─────────────────────────────────────────────────────────────────────────────
# Scheduled task — auto-resume after reboot
# ─────────────────────────────────────────────────────────────────────────────
function Register-ContinueTask {
    param([PSCustomObject]$s)
    $psArgs = "-ExecutionPolicy Bypass -NonInteractive -File `"$ScriptPath`"" +
              " -NodeIP `"$($s.NodeIP)`" -NodeHostname `"$($s.NodeHostname)`""
    $action    = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $psArgs
    $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $principal = New-ScheduledTaskPrincipal `
                     -UserId $env:USERNAME -RunLevel Highest -LogonType Interactive
    $settings  = New-ScheduledTaskSettingsSet `
                     -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
                     -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
    Log "Post-reboot continuation task registered (runs at next logon as $env:USERNAME)"
}

function Remove-ContinueTask {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Log "Continuation task removed"
    }
}

function Reboot-AndContinue {
    param([string]$Reason, [PSCustomObject]$s)
    Log "Scheduling reboot: $Reason"
    Register-ContinueTask -s $s
    Start-Sleep -Seconds 3
    Restart-Computer -Force
    exit 0
}

# ─────────────────────────────────────────────────────────────────────────────
# WSL bash execution helper
# Writes script to %TEMP% (accessible as /mnt/c/... inside WSL).
# Avoids all PowerShell quoting/escaping problems with complex bash scripts.
# NOTE: Inside @"..."@ heredocs, escape all bash vars as \$VAR.
#       PowerShell vars like $ProxyUrl expand as normal.
# ─────────────────────────────────────────────────────────────────────────────
function Invoke-Wsl {
    param(
        [string]$BashScript,
        [string]$User   = "root",
        [string]$Distro = $WslDistro
    )
    # Force LF line endings (bash requires them)
    $script = $BashScript -replace "`r`n", "`n"
    $tmpWin = [System.IO.Path]::Combine(
                  $env:TEMP,
                  "wslsetup_$([System.Guid]::NewGuid().ToString('N')).sh")
    [System.IO.File]::WriteAllText($tmpWin, $script,
        [System.Text.UTF8Encoding]::new($false))   # UTF-8, no BOM

    # C:\Users\Administrator\AppData\Local\Temp\xxx.sh
    # → /mnt/c/Users/Administrator/AppData/Local/Temp/xxx.sh
    $drive  = $tmpWin[0].ToString().ToLower()
    $rest   = $tmpWin.Substring(2).Replace('\', '/')
    $wslTmp = "/mnt/$drive$rest"

    try {
        wsl -d $Distro -u $User -- bash $wslTmp
        if ($LASTEXITCODE -ne 0) {
            throw "WSL script failed (exit $LASTEXITCODE)"
        }
    }
    finally {
        Remove-Item $tmpWin -ErrorAction SilentlyContinue
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# Phase 0 — Enable WSL2 Windows features
# ─────────────────────────────────────────────────────────────────────────────
function Phase0-EnableWslFeatures {
    param([PSCustomObject]$s)
    Step "Phase 0 — Enable WSL2 Windows features (dism)"

    $needReboot = $false

    $wslFeat = Get-WindowsOptionalFeature -Online `
                   -FeatureName Microsoft-Windows-Subsystem-Linux
    if ($wslFeat.State -ne "Enabled") {
        Log "Enabling Microsoft-Windows-Subsystem-Linux..."
        $r = Enable-WindowsOptionalFeature -Online `
                 -FeatureName Microsoft-Windows-Subsystem-Linux -NoRestart
        if ($r.RestartNeeded) { $needReboot = $true }
    } else { Log "WSL feature already enabled" }

    $vmpFeat = Get-WindowsOptionalFeature -Online `
                   -FeatureName VirtualMachinePlatform
    if ($vmpFeat.State -ne "Enabled") {
        Log "Enabling VirtualMachinePlatform..."
        $r = Enable-WindowsOptionalFeature -Online `
                 -FeatureName VirtualMachinePlatform -NoRestart
        if ($r.RestartNeeded) { $needReboot = $true }
    } else { Log "VirtualMachinePlatform already enabled" }

    $s.Phase = 1
    Save-State $s

    if ($needReboot) {
        Reboot-AndContinue "WSL features enabled — reboot required" -s $s
    } else {
        Log "Features already enabled, no reboot needed"
        Phase1-InstallAndConfigureWsl $s
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — WSL update, Ubuntu install, user, wsl.conf, .wslconfig,
#            wsl --shutdown, verify mirrored networking
# ─────────────────────────────────────────────────────────────────────────────
function Phase1-InstallAndConfigureWsl {
    param([PSCustomObject]$s)
    Step "Phase 1 — WSL update + Ubuntu 24.04 + WSL configuration"
    Remove-ContinueTask

    # Set WSL2 as default
    Log "Setting WSL2 as default version..."
    wsl --set-default-version 2
    if ($LASTEXITCODE -ne 0) { Die "wsl --set-default-version 2 failed" }

    # Update WSL kernel (needs internet — use proxy at Windows level)
    Log "Updating WSL kernel..."
    $env:HTTPS_PROXY = $ProxyUrl
    $env:HTTP_PROXY  = $ProxyUrl
    wsl --update   # non-zero = already up to date, non-fatal
    $env:HTTPS_PROXY = ""
    $env:HTTP_PROXY  = ""

    # Install Ubuntu-24.04 (--no-launch avoids interactive OOBE)
    $installed = (wsl -l --quiet 2>&1) | Where-Object { $_ -match "Ubuntu-24.04" }
    if (-not $installed) {
        Log "Installing Ubuntu-24.04 (--no-launch)..."
        wsl --install -d $WslDistro --no-launch
        if ($LASTEXITCODE -ne 0) { Die "wsl --install failed" }
    } else {
        Log "Ubuntu-24.04 already present"
    }

    # Create administrator user (run as root — no interactive prompt)
    Log "Creating WSL user '$WslUser'..."
    Invoke-Wsl -User "root" -BashScript @"
#!/bin/bash
set -euo pipefail
if ! id $WslUser &>/dev/null; then
    useradd -m -s /bin/bash $WslUser
    echo '${WslUser}:${WslPassword}' | chpasswd
    usermod -aG sudo,adm,cdrom,dip,plugdev,users $WslUser
    echo '$WslUser ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/$WslUser
    chmod 0440 /etc/sudoers.d/$WslUser
    echo "User $WslUser created"
else
    echo "User $WslUser already exists"
fi
"@

    # /etc/wsl.conf  (systemd, default user, no auto-hosts)
    Log "Writing /etc/wsl.conf..."
    Invoke-Wsl -User "root" -BashScript @'
#!/bin/bash
cat > /etc/wsl.conf << 'CONF'
[boot]
systemd=true

[user]
default=administrator

[interop]
appendWindowsPath = true

[network]
generateHosts = false
CONF
echo "wsl.conf written"
'@

    # C:\Users\Administrator\.wslconfig  (mirrored networking)
    $wslCfg = Join-Path $env:USERPROFILE ".wslconfig"
    Log "Writing .wslconfig → $wslCfg"
    "[wsl2]`nnetworkingMode=Mirrored`n" |
        Set-Content -Path $wslCfg -Encoding UTF8

    # Restart WSL so both config files take effect
    Log "Shutting down WSL (applying wsl.conf + .wslconfig)..."
    wsl --shutdown
    Log "Waiting 12 s for full WSL shutdown..."
    Start-Sleep -Seconds 12

    # ── Verify mirrored networking ────────────────────────────────────────────
    Log "Verifying mirrored networking..."
    $rawIP = (wsl -d $WslDistro -- hostname -I 2>&1)
    $wslIP = ($rawIP -split '\s+' | Where-Object { $_ -match '^\d' } |
              Select-Object -First 1)
    Log "hostname -I → '$rawIP'  (first token: '$wslIP')"

    if (-not ($wslIP -match '^10\.')) {
        Write-Host ""
        Write-Host "  *** MIRRORED NETWORKING NOT WORKING ***" -ForegroundColor Red
        Write-Host "  hostname -I returned: $rawIP" -ForegroundColor Red
        Write-Host ""
        Write-Host "  Diagnostics:" -ForegroundColor Yellow
        Write-Host "    blank output  → mirrored mode not activated yet" -ForegroundColor Yellow
        Write-Host "    172.x.x.x     → WSL still in NAT mode" -ForegroundColor Yellow
        Write-Host ""
        Write-Host "  Fixes to try:" -ForegroundColor Yellow
        Write-Host "    1. Confirm .wslconfig is at: $wslCfg" -ForegroundColor Yellow
        Write-Host "    2. Content must be exactly:" -ForegroundColor Yellow
        Write-Host "         [wsl2]" -ForegroundColor White
        Write-Host "         networkingMode=Mirrored" -ForegroundColor White
        Write-Host "    3. Run 'wsl --shutdown', wait 10 s, re-run this script" -ForegroundColor Yellow
        Write-Host "    4. Windows build must be >= 10.0.22621 (Win 11 22H2+)" -ForegroundColor Yellow
        Write-Host "    5. Check Windows Firewall isn't blocking WSL adapter" -ForegroundColor Yellow
        Write-Host ""
        Die "Fix mirrored networking then re-run. Script will resume at Phase 1."
    }
    Log "Mirrored networking OK: $wslIP" "OK"

    $s.Phase = 2
    Save-State $s
    Phase2-ProxyConfig $s
}

# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — Proxy configuration
# Must happen before any apt/wget/curl/pip inside WSL
# ─────────────────────────────────────────────────────────────────────────────
function Phase2-ProxyConfig {
    param([PSCustomObject]$s)
    Step "Phase 2 — Proxy configuration (apt + /etc/environment)"

    # /etc/apt/apt.conf.d/99proxy  — apt ignores /etc/environment non-interactively
    Log "Writing apt proxy config..."
    Invoke-Wsl -User "root" -BashScript @"
#!/bin/bash
cat > /etc/apt/apt.conf.d/99proxy << 'APTPROXY'
Acquire::http::Proxy  "$ProxyUrl";
Acquire::https::Proxy "$ProxyUrl";
APTPROXY
echo "apt proxy written: \$(cat /etc/apt/apt.conf.d/99proxy)"
"@

    # /etc/environment  — proxy + Ray bypass vars (all processes)
    Log "Writing /etc/environment..."
    Invoke-Wsl -User "root" -BashScript @"
#!/bin/bash
cat > /etc/environment << 'ENV'
PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games:/usr/local/games:/snap/bin"
http_proxy=http://10.205.122.201:8080
https_proxy=http://10.205.122.201:8080
HTTP_PROXY=http://10.205.122.201:8080
HTTPS_PROXY=http://10.205.122.201:8080
NO_PROXY=$NoProxy
no_proxy=$NoProxy
RAY_SERVE_PROXY_PREFER_LOCAL_NODE_ROUTING=0
RAY_grpc_enable_http_proxy=0
ENV
echo "/etc/environment written"
"@

    # /etc/hosts  (static — generateHosts=false in wsl.conf)
    $nodeIP   = $s.NodeIP
    $nodeHost = $s.NodeHostname
    Log "Writing /etc/hosts (node: $nodeIP $nodeHost)..."
    Invoke-Wsl -User "root" -BashScript @"
#!/bin/bash
cat > /etc/hosts << 'HOSTS'
127.0.0.1   localhost
$nodeIP   $nodeHost.localdomain   $nodeHost

# ONGC cluster nodes
10.208.211.62   PVLPOCC00WS011.localdomain   PVLPOCC00WS011
10.208.211.54   PVLPOCC00WS003.localdomain   PVLPOCC00WS003
10.208.211.59   PVLPOCC00WS008.localdomain   PVLPOCC00WS008
10.208.211.64   PVLPOCC00WS013.localdomain   PVLPOCC00WS013

::1     ip6-localhost ip6-loopback
fe00::0 ip6-localnet
ff00::0 ip6-mcastprefix
ff02::1 ip6-allnodes
ff02::2 ip6-allrouters
HOSTS
echo "/etc/hosts written"
"@

    $s.Phase = 3
    Save-State $s
    Phase3-PackagesCudaDocker $s
}

# ─────────────────────────────────────────────────────────────────────────────
# Phase 3 — apt base packages, CUDA 12.8 (WSL repo), Docker, SSH
# ─────────────────────────────────────────────────────────────────────────────
function Phase3-PackagesCudaDocker {
    param([PSCustomObject]$s)
    Step "Phase 3 — Base packages + CUDA Toolkit 12.8 + Docker + SSH"

    # ── Base packages ─────────────────────────────────────────────────────────
    Log "Installing base packages..."
    Invoke-Wsl -User "root" -BashScript @'
#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q \
    build-essential cmake git curl wget \
    openssh-server openssh-client sshpass \
    python3 python3-pip python3-venv \
    pkg-config libssl-dev \
    net-tools htop nvtop lshw \
    ca-certificates gnupg lsb-release
echo "Base packages done"
'@

    # ── CUDA Toolkit 12.8 via WSL-specific repo ───────────────────────────────
    # DO NOT use the regular linux/ubuntu repo — use wsl-ubuntu only.
    # The Windows host driver exposes libcuda.so via /usr/lib/wsl/lib/;
    # the toolkit here provides nvcc, cuBLAS, etc.
    Log "Adding CUDA WSL repo..."
    Invoke-Wsl -User "root" -BashScript @'
#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

if [ ! -f /etc/apt/sources.list.d/cuda-wsl-ubuntu-x86_64.list ]; then
    wget -q -O /tmp/cuda-keyring.deb \
        "https://developer.download.nvidia.com/compute/cuda/repos/wsl-ubuntu/x86_64/cuda-keyring_1.1-1_all.deb"
    dpkg -i /tmp/cuda-keyring.deb
    rm -f /tmp/cuda-keyring.deb
    apt-get update -q
    echo "CUDA WSL repo added"
else
    echo "CUDA WSL repo already present"
fi
'@

    Log "Installing cuda-toolkit-12-8..."
    Invoke-Wsl -User "root" -BashScript @'
#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
if ! dpkg -l cuda-toolkit-12-8 2>/dev/null | grep -q '^ii'; then
    apt-get install -y -q cuda-toolkit-12-8
    echo "CUDA Toolkit 12.8 installed"
else
    echo "CUDA Toolkit 12.8 already installed"
fi

# CUDA PATH for all login shells
if [ ! -f /etc/profile.d/cuda.sh ]; then
    cat > /etc/profile.d/cuda.sh << 'CUDA'
export PATH=/usr/local/cuda/bin:${PATH}
export LD_LIBRARY_PATH=/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}
CUDA
    chmod +x /etc/profile.d/cuda.sh
fi

# ldconfig for CUDA shared libs
echo '/usr/local/cuda/targets/x86_64-linux/lib' > /etc/ld.so.conf.d/000_cuda.conf
ldconfig
echo "CUDA paths configured"
'@

    # ── Docker ────────────────────────────────────────────────────────────────
    Log "Installing Docker..."
    Invoke-Wsl -User "root" -BashScript @"
#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

if ! command -v docker &>/dev/null; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo \"deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] \
        https://download.docker.com/linux/ubuntu \$(lsb_release -cs) stable\" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -q
    apt-get install -y -q docker.io docker-compose-plugin docker-buildx-plugin docker-compose
    echo \"Docker installed\"
else
    echo \"Docker already installed\"
fi

# docker group for $WslUser
usermod -aG docker $WslUser || true

# Enable services (systemd is active via wsl.conf systemd=true)
systemctl enable docker 2>/dev/null || true
systemctl start  docker 2>/dev/null || true
echo \"Docker service enabled\"
"@

    # ── SSH ───────────────────────────────────────────────────────────────────
    Log "Configuring SSH server..."
    Invoke-Wsl -User "root" -BashScript @'
#!/bin/bash
# Generate host keys if missing (first boot)
if [ ! -f /etc/ssh/ssh_host_rsa_key ]; then
    ssh-keygen -A
    echo "SSH host keys generated"
fi
systemctl enable ssh 2>/dev/null || true
systemctl restart ssh 2>/dev/null || true
echo "SSH active: $(systemctl is-active ssh)"
'@

    $s.Phase = 4
    Save-State $s
    Phase4-Verify $s
}

# ─────────────────────────────────────────────────────────────────────────────
# Phase 4 — Verification
# ─────────────────────────────────────────────────────────────────────────────
function Phase4-Verify {
    param([PSCustomObject]$s)
    Step "Phase 4 — Verification"

    $results = [ordered]@{}
    $fail    = 0

    # Helper: run WSL command, capture output, mark pass/fail
    function Check {
        param([string]$Label, [scriptblock]$Cmd, [string]$Expect = "")
        try {
            $out = & $Cmd 2>&1 | Out-String
            $out = $out.Trim()
            if ($Expect -and $out -notmatch $Expect) {
                $script:results[$Label] = "$out  ← FAIL (expected: $Expect)"
                $script:fail++
            } else {
                $script:results[$Label] = "$out  [OK]"
            }
        } catch {
            $script:results[$Label] = "ERROR: $_  [FAIL]"
            $script:fail++
        }
    }

    Check "WSL version" {
        wsl --version 2>&1 | Select-String "WSL version" | ForEach-Object { $_.Line.Trim() }
    } "2\."

    Check "Mirrored IP (10.*)" {
        $raw = (wsl -d $WslDistro -- hostname -I 2>&1)
        ($raw -split '\s+' | Where-Object { $_ -match '^\d' } | Select-Object -First 1)
    } "^10\."

    Check "nvidia-smi (GPU visible)" {
        wsl -d $WslDistro -- nvidia-smi `
            --query-gpu=name,driver_version,memory.total `
            --format=csv,noheader 2>&1
    } "RTX A4000"

    Check "nvidia-smi CUDA version" {
        wsl -d $WslDistro -- nvidia-smi `
            --query-gpu=name --format=csv,noheader 2>&1
        # Separately grab the CUDA column from full nvidia-smi header
        (wsl -d $WslDistro -- nvidia-smi 2>&1 | Select-String "CUDA Version") |
            ForEach-Object { $_.Line.Trim() }
    }

    Check "nvcc (CUDA compiler)" {
        wsl -d $WslDistro -- bash -c `
            'PATH=/usr/local/cuda/bin:$PATH nvcc --version 2>&1 | grep release'
    } "release 12\."

    Check "Docker version" {
        wsl -d $WslDistro -- docker --version 2>&1
    } "Docker version"

    Check "Docker service" {
        wsl -d $WslDistro -- systemctl is-active docker 2>&1
    } "active"

    Check "SSH service" {
        wsl -d $WslDistro -- systemctl is-active ssh 2>&1
    } "active"

    Check "Proxy in /etc/environment" {
        wsl -d $WslDistro -- bash -c 'grep -c http_proxy /etc/environment'
    } "^[1-9]"

    Check "apt proxy config" {
        wsl -d $WslDistro -- bash -c 'cat /etc/apt/apt.conf.d/99proxy'
    } "Proxy"

    # ── Print results ─────────────────────────────────────────────────────────
    Write-Host ""
    Write-Host ("═" * 60) -ForegroundColor $(if ($fail -eq 0) {"Green"} else {"Red"})
    Write-Host "  VERIFICATION RESULTS" -ForegroundColor White
    Write-Host ("═" * 60) -ForegroundColor $(if ($fail -eq 0) {"Green"} else {"Red"})
    $results.GetEnumerator() | ForEach-Object {
        $color = if   ($_.Value -match '\[FAIL\]|\bERROR\b') { "Red" }
                 elseif ($_.Value -match '\[OK\]')            { "Green" }
                 else                                          { "White" }
        Write-Host ("  {0,-30}  {1}" -f $_.Key, $_.Value) -ForegroundColor $color
    }
    Write-Host ("═" * 60) -ForegroundColor $(if ($fail -eq 0) {"Green"} else {"Red"})

    if ($fail -eq 0) {
        Write-Host ""
        Write-Host "  Base environment matches WS-11." -ForegroundColor Green
        Write-Host "  Next: LLM stack setup (llama.cpp, venvs, project)." -ForegroundColor Cyan
    } else {
        Write-Host ""
        Write-Host "  $fail check(s) failed. Review output above." -ForegroundColor Red
        Write-Host "  Re-run script to retry from current phase." -ForegroundColor Yellow
    }

    Write-Host ""
    Write-Host "  Log: $LogFile" -ForegroundColor DarkGray
    Write-Host ""

    $s.Phase = 99
    Save-State $s
    Remove-ContinueTask
    Log "Environment setup complete ($fail failures)" $(if ($fail -eq 0) {"OK"} else {"WARN"})
}

# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
New-Item -ItemType Directory -Path $SetupDir -Force | Out-Null

if ($Reset) {
    Remove-Item $StateFile -ErrorAction SilentlyContinue
    Remove-ContinueTask
    Log "State reset — run script again to start from Phase 0." "OK"
    exit 0
}

$state = Get-State

# Args override stale state (useful when resuming manually with new values)
if ($NodeIP)       { $state.NodeIP       = $NodeIP }
if ($NodeHostname) { $state.NodeHostname = $NodeHostname }

# Prompt only on Phase 0 (first run, interactive session)
if ($state.Phase -eq 0) {
    if (-not $state.NodeIP) {
        $state.NodeIP = Read-Host "This node's IP address  (e.g. 10.208.211.XX)"
    }
    if (-not $state.NodeHostname) {
        $state.NodeHostname = Read-Host "This node's hostname    (e.g. PVLPOCC00WS0XX)"
    }
}

Save-State $state
Log "━━━ ONGC Node Setup ━━━  Node: $($state.NodeIP) ($($state.NodeHostname))  Phase: $($state.Phase)"

switch ($state.Phase) {
    0  { Phase0-EnableWslFeatures         $state }
    1  { Phase1-InstallAndConfigureWsl    $state }
    2  { Phase2-ProxyConfig               $state }
    3  { Phase3-PackagesCudaDocker        $state }
    4  { Phase4-Verify                    $state }
    99 { Log "Setup already complete. Use -Reset to start over." "OK" }
    default { Die "Unknown phase: $($state.Phase)" }
}
