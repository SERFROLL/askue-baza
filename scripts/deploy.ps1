# Обновление системы на виртуалке с компьютера 3S-WS03.
# Перед запуском код должен быть загружен в GitHub (git push).
#   powershell -File scripts\deploy.ps1 -VmHost 192.168.x.x
param(
    [Parameter(Mandatory = $true)][string]$VmHost,
    [string]$User = "askue",
    [string]$Key = "$env:USERPROFILE\.ssh\askue_ed25519"
)
$remote = 'cd ~/askue-baza && git pull --ff-only && docker compose up -d --build && docker compose ps'
& ssh -i $Key -o IdentitiesOnly=yes "$User@$VmHost" $remote
exit $LASTEXITCODE
