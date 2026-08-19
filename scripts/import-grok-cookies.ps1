[CmdletBinding()]
param(
    [string]$OutputPath = "$HOME\grok_session.json"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$cookie = (Get-Clipboard -Raw).Trim()
if ([string]::IsNullOrWhiteSpace($cookie)) {
    throw "Clipboard is empty. Copy the Cookie header value from a grok.com request first."
}

# Accept either the raw value or a copied `Cookie: ...` header.
$cookie = $cookie -replace '(?im)^\s*Cookie:\s*', ''
$cookie = ($cookie -split "`r?`n" | Where-Object { $_.Trim() }) -join ' '
if ($cookie -notmatch '(^|;\s*)[^=;\s]+=') {
    throw "Clipboard does not look like a Cookie header. No file was written."
}

$parent = Split-Path -Parent $OutputPath
if ($parent) {
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
}

$payload = @{ cookies = $cookie } | ConvertTo-Json -Compress
$resolvedPath = [IO.Path]::GetFullPath($OutputPath)
[IO.File]::WriteAllText($resolvedPath, $payload)

# Do not print the cookie or the JSON payload.
Write-Host "Saved Grok session cookies to $resolvedPath"
Write-Host "Next: hermes quota refresh"
