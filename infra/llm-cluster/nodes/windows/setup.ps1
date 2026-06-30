# One-time Windows LLM-node bootstrap (192.168.1.13).
# Native Ollama uses the NVIDIA GPU directly — no WSL, no Docker.
# Run in an ELEVATED PowerShell (Run as Administrator):
#   powershell -ExecutionPolicy Bypass -File setup.ps1
#
# After this one-time setup, `edge deploy` (from WSL on this box) keeps the node
# refreshed via interop — no need to re-run this unless you reinstall Windows.

$ErrorActionPreference = "Stop"
$Model = if ($env:LLM_MODEL) { $env:LLM_MODEL } else { "llama3.2:3b" }

Write-Host "==> Installing Ollama (native Windows, NVIDIA GPU)..."
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements
}
# Resolve the full path — right after install 'ollama' isn't on PATH in this session.
$Ollama = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
if (-not (Test-Path $Ollama)) { $Ollama = "ollama" }  # fall back to PATH

Write-Host "==> Persisting the LAN bind (machine env OLLAMA_HOST=0.0.0.0)..."
[Environment]::SetEnvironmentVariable("OLLAMA_HOST", "0.0.0.0:11434", "Machine")

Write-Host "==> Opening the Windows Firewall for TCP 11434..."
if (-not (Get-NetFirewallRule -DisplayName "Ollama LAN 11434" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "Ollama LAN 11434" -Direction Inbound `
        -Action Allow -Protocol TCP -LocalPort 11434 | Out-Null
}

Write-Host "==> (Re)starting Ollama bound to the LAN..."
Get-Process ollama* -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
$env:OLLAMA_HOST = "0.0.0.0:11434"            # this session, so the child binds LAN now
Start-Process -FilePath $Ollama -ArgumentList "serve" -WindowStyle Hidden
Start-Sleep -Seconds 5

Write-Host "==> Pulling the shared model: $Model ..."
& $Ollama pull $Model

Write-Host "==> Self-check (should be 0.0.0.0):"
netstat -ano | Select-String ":11434" | Select-Object -First 2 | ForEach-Object { $_.Line.Trim() }
Write-Host "Done. Verify from another machine:  curl http://192.168.1.13:11434/api/tags"
Write-Host "Note: a reboot is the cleanest way to get a single boot-persistent LAN server"
Write-Host "      (the Ollama app then reads the machine env you just set)."
