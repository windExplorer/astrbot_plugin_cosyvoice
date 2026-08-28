# Pack astrbot_plugin_cosyvoice into an AstrBot-installable zip.
#
# Format follows the proven-working packages (pack.py / Explorer-style):
#   - files are wrapped inside a top-level folder named after the plugin
#     (e.g. astrbot_plugin_cosyvoice/main.py)
#   - explicit directory entries are included (e.g. astrbot_plugin_cosyvoice/core/)
#   - entry names use forward slashes
# Do NOT use Compress-Archive for this: it omits directory entries and
# AstrBot rejects/breaks on such archives.
#
# Usage:  .\pack_zip.ps1
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$name = Split-Path -Leaf $root

# Read version. MUST use -Encoding UTF8, otherwise the BOM-less UTF-8 file is read
# as ANSI/GBK and the Chinese comments corrupt line structure, hiding the version line.
$ver = ''
foreach ($l in Get-Content -LiteralPath (Join-Path $root 'metadata.yaml') -Encoding UTF8) {
    if ($l -match '^version\s*:\s*(\S+)') { $ver = $Matches[1]; break }
}
$ver = $ver -replace '^[vV]', ''
if (-not $ver) { throw 'Unable to read version from metadata.yaml' }

# Exclusions (kept in sync with the official pack.py)
$exDirs  = @('.git', '__pycache__', '.venv', 'node_modules', 'dist', 'frontend', '.reasonix')
$exFiles = @('pack.py', 'pack.sh', 'pack.bat', '.gitignore', 'pack_zip.ps1')

$allDirs = Get-ChildItem -LiteralPath $root -Directory -Recurse -ErrorAction SilentlyContinue |
    Where-Object {
        $parts = $_.FullName.Substring($root.Length).TrimStart('\') -split '\\'
        $exDirs -notcontains $parts[0]
    }
$allFiles = Get-ChildItem -LiteralPath $root -File -Recurse -ErrorAction SilentlyContinue |
    Where-Object {
        $rel = $_.FullName.Substring($root.Length).TrimStart('\')
        $parts = $rel -split '\\'
        if ($exDirs  -contains $parts[0])                    { return $false }
        if ($exFiles -contains $rel)                         { return $false }
        if ($_.Name -like '*.pyc' -or $_.Name -like '*.pyo') { return $false }
        return $true
    }

$outDir = Join-Path $root 'dist'
New-Item -ItemType Directory -Path $outDir -Force | Out-Null
$out = Join-Path $outDir ('{0}_v{1}_{2}.zip' -f $name, $ver, (Get-Date -Format 'yyyyMMdd_HHmmss'))

$fs = [System.IO.File]::Open($out, [System.IO.FileMode]::Create)
$zip = New-Object System.IO.Compression.ZipArchive($fs, [System.IO.Compression.ZipArchiveMode]::Create, $false)
try {
    # directory entries first
    foreach ($d in $allDirs) {
        $rel = $d.FullName.Substring($root.Length).TrimStart('\').Replace('\', '/')
        $entry = $zip.CreateEntry(($name + '/' + $rel + '/'))
    }
    # file entries
    $count = 0
    foreach ($f in $allFiles) {
        $rel = $f.FullName.Substring($root.Length).TrimStart('\').Replace('\', '/')
        $entry = $zip.CreateEntry(($name + '/' + $rel), [System.IO.Compression.CompressionLevel]::Optimal)
        $outStream = $entry.Open()
        $inStream = [System.IO.File]::OpenRead($f.FullName)
        try { $inStream.CopyTo($outStream) } finally { $inStream.Dispose(); $outStream.Dispose() }
        $count++
    }
} finally {
    $zip.Dispose()
    $fs.Dispose()
}

Write-Output ('PACKED=' + $out)
Write-Output ('FILES=' + $count)
$top = tar -tf $out | ForEach-Object { ($_ -split '/')[0] } | Sort-Object -Unique
Write-Output ('TOP_LEVEL=' + ($top -join ','))
if ($top -contains $name -and $top.Count -eq 1) {
    Write-Output 'OK: wrapped format matches proven-working packages.'
} else {
    Write-Output 'WARN: structure does NOT match the proven format! Check packaging.'
}
