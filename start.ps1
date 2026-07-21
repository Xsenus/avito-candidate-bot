param(
    [switch]$NoLaunch,
    [switch]$WithServer
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Set-Location $PSScriptRoot

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
chcp 65001 | Out-Null

function Get-PythonCommand {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return @('py')
    }

    if (Get-Command python -ErrorAction SilentlyContinue) {
        return @('python')
    }

    throw 'Python was not found. Install Python 3 and add it to PATH.'
}

function Get-EnvValue {
    param(
        [string]$Path,
        [string]$Key
    )

    if (-not (Test-Path $Path)) {
        return $null
    }

    foreach ($line in Get-Content $Path) {
        if ($line -match '^\s*#' -or $line -match '^\s*$') {
            continue
        }

        $parts = $line -split '=', 2
        if ($parts.Length -eq 2 -and $parts[0].Trim() -eq $Key) {
            return $parts[1].Trim().Trim('"')
        }
    }

    return $null
}

function Set-EnvValue {
    param(
        [string]$Path,
        [string]$Key,
        [string]$Value
    )

    $lines = @()
    $found = $false

    foreach ($line in Get-Content $Path) {
        if ($line -match '^\s*#' -or $line -match '^\s*$') {
            $lines += $line
            continue
        }

        $parts = $line -split '=', 2
        if ($parts.Length -eq 2 -and $parts[0].Trim() -eq $Key) {
            $lines += "$Key=$Value"
            $found = $true
        }
        else {
            $lines += $line
        }
    }

    if (-not $found) {
        $lines += "$Key=$Value"
    }

    Set-Content -Path $Path -Value $lines
}

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
}

$values = @{
    'AVITO_CLIENT_ID' = $env:AVITO_CLIENT_ID
    'AVITO_CLIENT_SECRET' = $env:AVITO_CLIENT_SECRET
    'AVITO_USER_ID' = $env:AVITO_USER_ID
    'AVITO_WEBHOOK_URL' = $env:AVITO_WEBHOOK_URL
    'YANDEX_FORM_URL' = $env:YANDEX_FORM_URL
    'AVITO_BASE_URL' = $env:AVITO_BASE_URL
}

$defaults = @{
    'AVITO_BASE_URL' = 'https://api.avito.ru'
}

foreach ($key in $values.Keys) {
    $current = Get-EnvValue -Path .env -Key $key
    if ([string]::IsNullOrWhiteSpace($current)) {
        $current = $values[$key]
    }

    if ([string]::IsNullOrWhiteSpace($current)) {
        $default = $defaults[$key]
        $prompt = "Введите $key"
        if ($default) {
            $prompt = "$prompt (по умолчанию: $default)"
        }

        $value = Read-Host $prompt
        if ([string]::IsNullOrWhiteSpace($value)) {
            if ($default) {
                $value = $default
            }
            else {
                $value = ''
            }
        }
    }
    else {
        $value = $current
    }

    if ($key -eq 'AVITO_WEBHOOK_URL' -and [string]::IsNullOrWhiteSpace($value)) {
        $value = 'https://your-domain.example/webhook/avito'
    }

    if ($key -eq 'USE_WEBHOOK' -and [string]::IsNullOrWhiteSpace($value)) {
        $value = 'false'
    }

    if ($key -eq 'YANDEX_FORM_URL' -and [string]::IsNullOrWhiteSpace($value)) {
        $value = 'https://forms.yandex.ru/'
    }

    if ($key -eq 'AVITO_BASE_URL' -and [string]::IsNullOrWhiteSpace($value)) {
        $value = $defaults[$key]
    }

    Set-EnvValue -Path .env -Key $key -Value $value
}

$pythonCommand = Get-PythonCommand
$pythonExe = if ($pythonCommand[0] -eq 'py') { 'py' } else { 'python' }
$pythonArgs = @()

Write-Host 'Installing dependencies...' -ForegroundColor Green
& $pythonExe @($pythonArgs + @('-m', 'pip', 'install', '-r', 'requirements.txt')) | Out-Null

try {
    & $pythonExe @($pythonArgs + @('-c', 'import dotenv, fastapi, uvicorn, requests; print("deps-ok")')) | Out-Null
}
catch {
    Write-Warning 'Dependency check failed. Please rerun the script.'
}

if ($NoLaunch) {
    Write-Host 'Configuration is ready. Launch was skipped because -NoLaunch was passed.' -ForegroundColor Yellow
    return
}

if ($WithServer) {
    $port = (& $pythonExe @($pythonArgs + @('.\set_port.py'))).Trim()
    Write-Host 'Starting the server...' -ForegroundColor Green
    Write-Host ("Address: http://127.0.0.1:{0}" -f $port)
    Start-Process -FilePath $pythonExe -ArgumentList @($pythonArgs + @('-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', $port)) -WorkingDirectory $PSScriptRoot
}

Write-Host 'Starting the Avito poller without webhooks...' -ForegroundColor Green
Start-Process -FilePath $pythonExe -ArgumentList @($pythonArgs + @('poller.py')) -WorkingDirectory $PSScriptRoot
Write-Host 'The poller window is running. The bot works via Avito polling.' -ForegroundColor Green
