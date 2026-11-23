# Обновление системы на виртуалке с компьютера 3S-WS03.
# Код уходит на виртуалку напрямую (git push в её рабочую копию), GitHub виртуалке не нужен.
#   powershell -File scripts\deploy.ps1
param(
    [string]$VmHost = "192.168.107.10",
    [string]$User = "admin3s",
    [string]$Key = "C:/Users/frolkov/.ssh/askue_ed25519",
    [string]$Source = "192.168.105.101"   # адрес 3S-WS03 в сети базы (на ПК работает VPN)
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$ssh = "C:/Windows/System32/OpenSSH/ssh.exe -i $Key -o IdentitiesOnly=yes -o BatchMode=yes -b $Source"
$env:GIT_SSH_COMMAND = $ssh
git push origin main
git push "$User@${VmHost}:askue-baza" main
$remote = 'cd ~/askue-baza && docker compose up -d --build --quiet-pull && docker compose ps'
cmd /c "$ssh $User@$VmHost `"$remote`""
exit $LASTEXITCODE
