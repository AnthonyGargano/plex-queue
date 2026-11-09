$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    py -3 -m venv .venv
}
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

if (-not (Test-Path ".\config.yaml")) {
    Copy-Item ".\config.example.yaml" ".\config.yaml"
    Write-Output "Created config.yaml from the example."
}

$key = Join-Path $env:USERPROFILE ".ssh\plex-queue_ed25519"
if (-not (Test-Path $key)) {
    New-Item -ItemType Directory -Force -Path (Split-Path $key) | Out-Null
    & ssh-keygen -t ed25519 -f $key -q -N ([string]::Empty)
    if ($LASTEXITCODE -ne 0) { throw "ssh-keygen failed" }
}

Write-Output ""
Write-Output "1. Put the BotFather token and a claim secret in config.yaml."
Write-Output "2. Install the SSH key on the VM. Type the password at the prompt; do not pipe the key into ssh."
Write-Output "ssh ttt@172.30.161.89 `"mkdir -p ~/.ssh && chmod 700 ~/.ssh && echo '$(Get-Content $key.pub -Raw)' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys`""
Write-Output "3. The Deluge password is the middle field of the localclient line in ~/.config/deluge/auth."
Write-Output "4. Test the connection:"
Write-Output ".\.venv\Scripts\python.exe -m plex_queue check"
Write-Output "5. Start the bot:"
Write-Output ".\.venv\Scripts\python.exe -m plex_queue"
