# 从 .env 读取配置后启动服务（Windows）
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".env")) {
    Write-Error "缺少 .env，请先复制 .env.example 并填入配置"
    exit 1
}

Get-Content ".env" -Encoding UTF8 | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
        $i = $line.IndexOf("=")
        $k = $line.Substring(0, $i).Trim()
        $v = $line.Substring($i + 1).Trim()
        [Environment]::SetEnvironmentVariable($k, $v, "Process")
    }
}

$port = if ($env:PORT) { $env:PORT } else { "8000" }
python -m uvicorn main:app --host 0.0.0.0 --port $port
