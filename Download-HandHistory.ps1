param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[A-Za-z0-9_-]{1,64}$')]
    [string]$Room,
    [string]$OutFile
)

$ErrorActionPreference = 'Stop'
$roomCode = $Room.ToUpperInvariant()
$keyFile = Join-Path $env:LOCALAPPDATA 'Poker Online\history-export-key.dpapi'
if (-not (Test-Path -LiteralPath $keyFile)) {
    throw 'This Windows account does not have the Poker Online history export key.'
}
if (-not $OutFile) {
    $OutFile = "poker-$roomCode-hands-$(Get-Date -Format 'yyyyMMdd-HHmmss').zip"
}
$destination = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutFile)
if (Test-Path -LiteralPath $destination) {
    throw "The output file already exists: $destination"
}
$temporary = "$destination.$([guid]::NewGuid().ToString('N')).tmp"
$credential = Get-Content -LiteralPath $keyFile -Raw | ConvertTo-SecureString
$token = [System.Net.NetworkCredential]::new('', $credential).Password
try {
    Invoke-WebRequest -Uri "https://texas-poker-38bu.onrender.com/history/archive?room=$roomCode" `
        -Headers @{Authorization = "Bearer $token"} -OutFile $temporary
    Move-Item -LiteralPath $temporary -Destination $destination
    Get-Item -LiteralPath $destination
}
finally {
    if (Test-Path -LiteralPath $temporary) {
        Remove-Item -LiteralPath $temporary
    }
}
