param(
  [string]$ContainerName = "thoth-postgres",
  [string]$Database = "thoth",
  [string]$User = "thoth",
  [string]$Password = "",
  [int]$Port = 55432,
  [string]$Image = "postgres:16",
  [int]$ReadyTimeoutSeconds = 600
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($Password)) {
  $Password = [Guid]::NewGuid().ToString("N")
}
if ($User -notmatch '^[A-Za-z_][A-Za-z0-9_]*$' -or $Database -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
  throw "Use simple PostgreSQL user/database identifiers."
}
if ($Password -match '[:/@?#\s]') { throw "Password must be URL-safe; use an alphanumeric generated password." }

function Invoke-PostgresSql([string]$Sql) {
  docker exec $ContainerName psql `
    --username $User `
    --dbname $Database `
    --set ON_ERROR_STOP=1 `
    --tuples-only `
    --no-align `
    --command $Sql
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to configure Postgres container $ContainerName."
  }
}

function Wait-PostgresReady {
  $deadline = (Get-Date).AddSeconds([Math]::Max(30, $ReadyTimeoutSeconds))
  while ((Get-Date) -lt $deadline) {
    $running = docker inspect --format "{{.State.Running}}" $ContainerName 2>$null
    if ($LASTEXITCODE -ne 0 -or ([string]$running).Trim().ToLowerInvariant() -ne "true") {
      throw "Postgres container $ContainerName exited before becoming ready."
    }
    docker exec $ContainerName pg_isready --username $User --dbname $Database *> $null
    if ($LASTEXITCODE -eq 0) {
      return
    }
    Start-Sleep -Seconds 1
  }
  throw "Postgres container $ContainerName did not become ready within $ReadyTimeoutSeconds seconds."
}

function Enable-QueryStatistics {
  $configuredLibraries = (Invoke-PostgresSql @"
SELECT COALESCE(
  (SELECT setting FROM pg_file_settings WHERE name = 'shared_preload_libraries' ORDER BY seqno DESC LIMIT 1),
  current_setting('shared_preload_libraries')
);
"@).Trim()
  $libraries = @($configuredLibraries -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
  if ($libraries -notcontains "pg_stat_statements") {
    $libraries += "pg_stat_statements"
    $setting = ($libraries -join ',').Replace("'", "''")
    Invoke-PostgresSql "ALTER SYSTEM SET shared_preload_libraries = '$setting';" | Out-Null
    Invoke-PostgresSql "SELECT pg_reload_conf();" | Out-Null
    Write-Host "Staged pg_stat_statements for the next supervised Postgres restart." -ForegroundColor Yellow
  }
  Invoke-PostgresSql "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;" | Out-Null
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  throw "Docker is required for the local Postgres helper. Install/start Docker Desktop or provide THOTH_DATABASE_URL for another Postgres."
}

$existing = docker ps -a --filter "name=^/$ContainerName$" --format "{{.Names}}"
if ($LASTEXITCODE -ne 0) {
  throw "Docker is installed but the daemon is not reachable. Start Docker Desktop and rerun this script."
}
if (-not $existing) {
  docker run `
    --name $ContainerName `
    -e POSTGRES_DB=$Database `
    -e POSTGRES_USER=$User `
    -e POSTGRES_PASSWORD=$Password `
    -p "127.0.0.1:${Port}:5432" `
    -v "${ContainerName}-data:/var/lib/postgresql/data" `
    -d $Image | Out-Host
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to create Postgres container."
  }
} else {
  $running = docker ps --filter "name=^/$ContainerName$" --format "{{.Names}}"
  if ($LASTEXITCODE -ne 0) {
    throw "Docker is installed but the daemon is not reachable. Start Docker Desktop and rerun this script."
  }
  if (-not $running) {
    docker start $ContainerName | Out-Host
    if ($LASTEXITCODE -ne 0) {
      throw "Failed to start Postgres container $ContainerName."
    }
  }
}

Wait-PostgresReady
Enable-QueryStatistics

Write-Host "Postgres is available." -ForegroundColor Green
if (-not $existing) {
  Write-Host "THOTH_DATABASE_URL=postgresql+psycopg://${User}:${Password}@127.0.0.1:${Port}/${Database}"
} else {
  Write-Host "Existing container credentials are unchanged. Keep your existing THOTH_DATABASE_URL."
}
