# Single-command bootstrap for the core stack (Windows).
#
# Deliberately not a Makefile: `make` is not installed on the target host, and
# a documented command that does not run is worse than no command at all.

[CmdletBinding()]
param(
    [switch]$Rebuild,
    [switch]$Down
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$compose = Join-Path $root 'infra/compose/docker-compose.yml'
$envFile = Join-Path $root 'infra/env/.env'
$envExample = Join-Path $root 'infra/env/.env.example'

function Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }
function Warn($message) { Write-Host "!!  $message" -ForegroundColor Yellow }

if ($Down) {
    Step 'Stopping the stack'
    docker compose --env-file $envFile -f $compose down
    exit 0
}

# 1. Environment
if (-not (Test-Path $envFile)) {
    Step 'Creating infra/env/.env from the template'
    Copy-Item $envExample $envFile
    Warn 'POSTGRES_PASSWORD is empty. Edit infra/env/.env, then run this again.'
    exit 1
}
if (-not (Select-String -Path $envFile -Pattern '^POSTGRES_PASSWORD=.+' -Quiet)) {
    Warn 'POSTGRES_PASSWORD is not set in infra/env/.env.'
    exit 1
}

# 2. Runtime directories. uploads/prod is created but never read by agents.
Step 'Preparing data and upload directories'
foreach ($dir in 'data/inbox', 'data/store', 'data/quarantine', 'uploads/dummy', 'uploads/prod') {
    New-Item -ItemType Directory -Force (Join-Path $root $dir) | Out-Null
}

# 3. Services, then migrations (the app service applies them on start).
Step 'Starting Postgres and the pipeline runtime'
$buildArgs = @('--env-file', $envFile, '-f', $compose, 'up', '-d')
if ($Rebuild) { $buildArgs += '--build' }
docker compose @buildArgs
if ($LASTEXITCODE -ne 0) { throw 'docker compose up failed' }

# 4. Health
Step 'Verifying the pipeline is reachable'
docker compose --env-file $envFile -f $compose exec -T app finstone status
if ($LASTEXITCODE -ne 0) { throw 'health check failed' }

Write-Host ''
Step 'Ready'
Write-Host '  Ingest the dummy documents:'
Write-Host "    docker compose --env-file $envFile -f $compose exec app finstone run --profile dummy"
Write-Host '  Explain a single document:'
Write-Host "    docker compose --env-file $envFile -f $compose exec app finstone doctor <path>"
