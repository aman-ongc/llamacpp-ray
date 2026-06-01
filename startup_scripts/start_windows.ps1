#Requires -Version 5.1
<#
.SYNOPSIS
    ONGC LLM Inference Platform — Startup Script (Windows / PowerShell)

.DESCRIPTION
    Starts the full platform on the controller node (WS-11) from a Windows
    machine on the ONGC intranet.

    Two modes:
      - LOCAL  : Run on WS-11 itself. Uses wsl.exe to drive WSL2 commands.
      - REMOTE : Run from any Windows machine. SSH into WS-11 via PuTTY/plink.

    Requirements (LOCAL mode):
      - WSL2 with Ubuntu-24.04 distro
      - Docker Desktop or Docker Engine in WSL2

    Requirements (REMOTE mode):
      - plink.exe on PATH  (from PuTTY: https://www.putty.org/)
      - OR OpenSSH for Windows (Settings > Optional Features > OpenSSH Client)

.EXAMPLE
    # Local mode (on WS-11):
    .\start_windows.ps1

    # Remote mode (from another Windows PC):
    .\start_windows.ps1 -Mode Remote
#>

param(
    [ValidateSet("Local", "Remote")]
    [string]$Mode = "Local",

    [string]$ControllerIP   = "10.208.211.62",
    [string]$SshUser        = "administrator",
    [string]$SshPass        = "Ongc@1234",
    [string]$WslDistro      = "Ubuntu-24.04",
    [string]$ProjectDir     = "/home/administrator/projects/llm-inference-service",
    [string]$VenvDir        = "/mnt/d/VirtualEnvironments/llm-platform",
    [int]   $LlamaPort      = 8080,
    [int]   $ServePort      = 8001,
    [int]   $GatewayPort    = 18000,
    [int]   $NginxPort      = 10080
)

$ErrorActionPreference = "Stop"

# ── Colours ───────────────────────────────────────────────────────────────────
function Write-Info  { param([string]$Msg) Write-Host "[INFO]  $Msg" -ForegroundColor Cyan }
function Write-Ok    { param([string]$Msg) Write-Host "[OK]    $Msg" -ForegroundColor Green }
function Write-Warn  { param([string]$Msg) Write-Host "[WARN]  $Msg" -ForegroundColor Yellow }
function Write-Fail  { param([string]$Msg) Write-Host "[ERROR] $Msg" -ForegroundColor Red; exit 1 }

# ── HTTP health check ─────────────────────────────────────────────────────────
function Wait-ForHttp {
    param([string]$Url, [string]$Label, [int]$Retries = 20)
    for ($i = 0; $i -lt $Retries; $i++) {
        try {
            $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3 `
                    -Proxy "" -ErrorAction Stop
            if ($resp.StatusCode -lt 400) {
                Write-Ok "$Label is up"
                return
            }
        } catch { }
        Start-Sleep 3
    }
    Write-Fail "$Label did not respond at $Url after $($Retries * 3)s"
}

# ── Command runners ───────────────────────────────────────────────────────────
function Invoke-Wsl {
    param([string]$Cmd)
    # Run inside WSL2 Ubuntu on the local machine (LOCAL mode).
    $result = wsl.exe -d $WslDistro -e bash -c $Cmd
    return $result
}

function Invoke-Remote {
    param([string]$Cmd)
    # Run on remote controller via SSH (REMOTE mode).
    # Prefers OpenSSH; falls back to plink.exe.
    $escaped = $Cmd -replace '"', '\"'
    if (Get-Command "ssh.exe" -ErrorAction SilentlyContinue) {
        $env:SSHPASS = $SshPass
        $output = ssh.exe -o StrictHostKeyChecking=no `
                          -o ConnectTimeout=10 `
                          "${SshUser}@${ControllerIP}" $Cmd 2>&1
    } elseif (Get-Command "plink.exe" -ErrorAction SilentlyContinue) {
        $output = plink.exe -ssh -pw $SshPass -batch `
                            "${SshUser}@${ControllerIP}" $Cmd 2>&1
    } else {
        Write-Fail "No SSH client found. Install OpenSSH (Windows Optional Features) or PuTTY plink.exe."
    }
    return $output
}

function Run {
    param([string]$Cmd)
    if ($Mode -eq "Local") {
        return Invoke-Wsl $Cmd
    } else {
        return Invoke-Remote $Cmd
    }
}

# ── Startup ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "╔══════════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║   ONGC LLM Inference Platform — Startup ($Mode)   ║" -ForegroundColor Cyan
Write-Host "╚══════════════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

# ── Connectivity check ────────────────────────────────────────────────────────
if ($Mode -eq "Remote") {
    Write-Info "Testing SSH connectivity to ${ControllerIP}..."
    $ping = Test-Connection -ComputerName $ControllerIP -Count 1 -Quiet
    if (-not $ping) { Write-Fail "Cannot ping ${ControllerIP}. Check network/VPN." }
    $sshTest = Run "echo SSH_OK"
    if ($sshTest -notmatch "SSH_OK") { Write-Fail "SSH to ${ControllerIP} failed." }
    Write-Ok "Controller reachable"
} else {
    Write-Info "Checking WSL2 distro '$WslDistro'..."
    $distros = wsl.exe -l --quiet 2>&1
    if ($distros -notmatch $WslDistro) {
        Write-Fail "WSL2 distro '$WslDistro' not found. Run: wsl --install -d Ubuntu-24.04"
    }
    Write-Ok "WSL2 distro available"
}

# ── Step 1: Docker Compose ────────────────────────────────────────────────────
Write-Info "Step 1/4 — Starting Docker Compose stack..."
$dockerCmd = @"
export no_proxy='localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in'
export NO_PROXY="${'$'}no_proxy"
cd '$ProjectDir'
printf '${SshPass}\n' | sudo -S docker compose up -d 2>&1 | grep -E 'Started|Running|Created|healthy|error' || true
"@
$out = Run $dockerCmd
Write-Host $out

Write-Info "Waiting for Gateway container..."
Wait-ForHttp "http://${ControllerIP}:${GatewayPort}/health" "Gateway" 30
Write-Ok "Docker stack ready"

# ── Step 2: llama.cpp server ──────────────────────────────────────────────────
Write-Info "Step 2/4 — Checking llama.cpp server..."
try {
    $resp = Invoke-WebRequest -Uri "http://${ControllerIP}:${LlamaPort}/health" `
            -UseBasicParsing -TimeoutSec 3 -Proxy "" -ErrorAction Stop
    Write-Ok "llama.cpp already running on port $LlamaPort"
} catch {
    Write-Warn "llama.cpp not running — starting..."
    $llamaCmd = @"
export no_proxy='localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in'
export NO_PROXY="${'$'}no_proxy"
nohup /home/administrator/projects/local_llm/llama.cpp/build/bin/llama-server \
    -m /mnt/d/Models/Qwen3.6-35B-A3B-GGUF-MTP-Q4/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf \
    --mmproj /mnt/d/Models/Qwen3.6-35B-A3B-GGUF-MTP-Q4/mmproj-F16.gguf \
    -ngl 999 -c 65536 \
    --host ${ControllerIP} --port ${LlamaPort} \
    --parallel 2 --no-context-shift \
    --flash-attn on \
    --cache-type-k q8_0 --cache-type-v q8_0 \
    --cont-batching \
    --spec-type draft-mtp --spec-draft-n-max 4 \
    > /tmp/llama-server.log 2>&1 &
echo llama-started
"@
    $out = Run $llamaCmd
    Write-Info "Model loading — this can take 30-90s..."
    Wait-ForHttp "http://${ControllerIP}:${LlamaPort}/health" "llama.cpp" 60
}

# ── Step 3: Ray head + Serve ──────────────────────────────────────────────────
Write-Info "Step 3/4 — Starting Ray head and Ray Serve..."
$rayCmd = @"
export no_proxy='localhost,127.0.0.1,10.0.0.0/8,.ongc.co.in'
export NO_PROXY="${'$'}no_proxy"
export RAY_grpc_enable_http_proxy=0
source '${VenvDir}/bin/activate'
cd '${ProjectDir}'
bash scripts/start_controller.sh
"@
$out = Run $rayCmd
Write-Host $out
Wait-ForHttp "http://${ControllerIP}:${ServePort}/health" "Ray Serve" 20

# ── Step 4: End-to-end verify ─────────────────────────────────────────────────
Write-Info "Step 4/4 — End-to-end check through NGINX..."
Wait-ForHttp "http://${ControllerIP}:${NginxPort}/health" "NGINX → Gateway" 15

# ── Summary ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "╔══════════════════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "║              Platform is UP                         ║" -ForegroundColor Green
Write-Host "╚══════════════════════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""
Write-Host "  API endpoint  " -NoNewline; Write-Host "http://${ControllerIP}:${NginxPort}/v1/chat/completions" -ForegroundColor Cyan
Write-Host "  Grafana        " -NoNewline; Write-Host "http://${ControllerIP}:13000   (admin / ongc1234)" -ForegroundColor Cyan
Write-Host "  Prometheus     " -NoNewline; Write-Host "http://${ControllerIP}:9090" -ForegroundColor Cyan
Write-Host "  Ray Dashboard  " -NoNewline; Write-Host "http://${ControllerIP}:8265" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Generate API key:" -ForegroundColor Yellow
if ($Mode -eq "Local") {
    Write-Host "    wsl.exe -d $WslDistro -e bash -c `"printf '${SshPass}\n' | sudo -S docker compose -f ${ProjectDir}/docker-compose.yml exec -T gateway python scripts/generate_api_key.py admin`""
} else {
    Write-Host "    ssh ${SshUser}@${ControllerIP} `"printf '${SshPass}\n' | sudo -S docker compose -f ${ProjectDir}/docker-compose.yml exec -T gateway python scripts/generate_api_key.py admin`""
}
Write-Host ""
