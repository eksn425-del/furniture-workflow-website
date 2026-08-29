[CmdletBinding()]
param(
    [string]$Root = ""
)

$Root = if ($Root) { $Root } else { Split-Path -Parent $PSScriptRoot }
$resolvedRoot = (Resolve-Path -LiteralPath $Root).Path
$ignoredDirectories = @('node_modules', '.next', '__pycache__', '.pytest_cache', '.git', 'output', 'data', '.venv')
$textExtensions = @('.md', '.txt', '.json', '.toml', '.ps1', '.py', '.ts', '.tsx', '.mjs', '.css', '.yml', '.yaml', '.example')
$secretPatterns = @(
    @{ Name = 'AWS access key'; Pattern = 'AKIA[0-9A-Z]{16}' },
    @{ Name = 'OpenAI-style key'; Pattern = 'sk-[A-Za-z0-9]{10,}' },
    @{ Name = 'Bearer credential'; Pattern = 'Bearer\s+[A-Za-z0-9._-]{16,}' },
    @{ Name = 'Private key'; Pattern = 'BEGIN\s+(RSA|EC|OPENSSH)\s+PRIVATE KEY' }
)

$findings = [System.Collections.Generic.List[string]]::new()
$files = Get-ChildItem -LiteralPath $resolvedRoot -Recurse -Force -File | Where-Object {
    $relative = $_.FullName.Substring($resolvedRoot.Length).TrimStart('\', '/')
    $parts = $relative -split '[\\/]'
    ($parts | Where-Object { $ignoredDirectories -contains $_ }).Count -eq 0 -and
    ($textExtensions -contains $_.Extension.ToLowerInvariant())
}

foreach ($file in $files) {
    $relative = $file.FullName.Substring($resolvedRoot.Length).TrimStart('\', '/')
    foreach ($rule in $secretPatterns) {
        $hit = Select-String -LiteralPath $file.FullName -Pattern $rule.Pattern -Quiet -ErrorAction SilentlyContinue
        if ($hit) { $findings.Add("$($rule.Name): $relative") }
    }
}

$forbiddenNames = @('.env.local', '.env.production', 'control_plane.sqlite3')
foreach ($file in Get-ChildItem -LiteralPath $resolvedRoot -Recurse -Force -File) {
    if ($forbiddenNames -contains $file.Name) {
        $findings.Add("forbidden runtime file: $($file.FullName.Substring($resolvedRoot.Length).TrimStart('\', '/'))")
    }
}

if ($findings.Count -gt 0) {
    $findings | ForEach-Object { Write-Error $_ }
    exit 1
}

Write-Output "PUBLIC_TREE_CHECK=PASS"
Write-Output "FILES_SCANNED=$($files.Count)"
