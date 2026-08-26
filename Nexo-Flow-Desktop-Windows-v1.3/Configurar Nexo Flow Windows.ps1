$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Base = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } elseif ($env:APPDATA) { $env:APPDATA } else { Join-Path $HOME "AppData\Local" }
$Support = Join-Path $Base "Nexo Flow"
$EnvFile = Join-Path $Support ".env"
New-Item -ItemType Directory -Path $Support -Force | Out-Null

if (-not (Test-Path -LiteralPath $EnvFile)) {
    $Example = Join-Path $ProjectRoot ".env.example"
    $Text = if (Test-Path -LiteralPath $Example) { Get-Content -Raw -LiteralPath $Example } else { "APP_ENV=development`n" }
    $Bytes = New-Object byte[] 48
    $Generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $Generator.GetBytes($Bytes) } finally { $Generator.Dispose() }
    $Secret = [Convert]::ToBase64String($Bytes).Replace("+", "-").Replace("/", "_").TrimEnd("=")
    $Text = [regex]::Replace($Text, '(?m)^APP_SECRET=.*$', "APP_SECRET=$Secret")
    $Text = [regex]::Replace($Text, '(?m)^(DATABASE_URL|PUBLIC_BASE_URL)=.*\r?\n?', '')
    [System.IO.File]::WriteAllText($EnvFile, $Text, [System.Text.UTF8Encoding]::new($false))
}

Start-Process notepad.exe -ArgumentList $EnvFile
