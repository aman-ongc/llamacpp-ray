#Requires -Version 5.1

param(
    [ValidateSet("Local", "Remote")]
    [string]$Mode = "Local",
    [string]$ControllerIP = "10.208.211.62",
    [string]$SshUser = "administrator",
    [string]$SshPass = "Ongc@1234",
    [string]$WslDistro = "Ubuntu-24.04",
    [string]$ProjectDir = "/home/administrator/projects/llm-inference-service"
)

$ErrorActionPreference = "Stop"

function Invoke-LocalStop {
    wsl.exe -d $WslDistro -e bash -lc "cd '$ProjectDir' && bash startup_scripts/stop_linux.sh"
}

function Invoke-RemoteStop {
    if (Get-Command ssh.exe -ErrorAction SilentlyContinue) {
        ssh.exe -o StrictHostKeyChecking=no "${SshUser}@${ControllerIP}" "cd '$ProjectDir' && bash startup_scripts/stop_linux.sh"
        return
    }
    if (Get-Command plink.exe -ErrorAction SilentlyContinue) {
        plink.exe -ssh -pw $SshPass -batch "${SshUser}@${ControllerIP}" "cd '$ProjectDir' && bash startup_scripts/stop_linux.sh"
        return
    }
    throw "No SSH client found. Install OpenSSH Client or PuTTY plink.exe."
}

if ($Mode -eq "Local") {
    Invoke-LocalStop
} else {
    Invoke-RemoteStop
}
