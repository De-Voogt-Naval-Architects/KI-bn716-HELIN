<#
Dev edge node for the DataLogger extractor, in a Multipass VM (Windows host).

  .\devnode\devnode.ps1 up      create the VM if needed, copy the repo in, lock + stubs + tests, start the stack
  .\devnode\devnode.ps1 sync    copy module/ and devnode/ in again and rebuild
  .\devnode\devnode.ps1 test    poetry lock + proto stubs + pytest in the VM; copies poetry.lock and stubs back
  .\devnode\devnode.ps1 logs    follow the extractor's logs
  .\devnode\devnode.ps1 files   list the CSVs the extractors wrote, with row counts
  .\devnode\devnode.ps1 real    run the container once against the real DataLoggerGRPC.exe
                                (settings: DATALOGGER_ADDR, REAL_START, REAL_END, REAL_RATE in devnode\.env)
  .\devnode\devnode.ps1 shell   open a shell in the VM
  .\devnode\devnode.ps1 down    stop the stack (VM keeps running)
#>
param([ValidateSet("up", "sync", "test", "logs", "files", "real", "shell", "down")][string]$Command = "up")

$ErrorActionPreference = "Stop"
$VM = "helin-edge"
$Repo = Split-Path -Parent $PSScriptRoot
$Remote = "/home/ubuntu/app"

function Invoke-Multipass { & multipass @args; if ($LASTEXITCODE) { throw "multipass $($args -join ' ') failed ($LASTEXITCODE)" } }
function vm([string]$cmd) { Invoke-Multipass exec $VM '--' bash -lc $cmd }   # bare -- would be eaten by PowerShell
function compose([string]$c) { vm "cd $Remote/devnode && docker compose $c" }

function Ensure-Vm {
    # `multipass info` on a missing VM writes to stderr, which PS 5.1 turns into a
    # terminating error under ErrorActionPreference=Stop - use the list instead.
    $row = (& multipass list --format csv) | ConvertFrom-Csv | Where-Object { $_.Name -eq $VM }
    if (-not $row) {
        Write-Host "Creating VM $VM (Ubuntu 24.04, 4 CPU, 8 GB, 40 GB)..."
        Invoke-Multipass launch 24.04 --name $VM --cpus 4 --memory 8G --disk 40G --cloud-init (Join-Path $PSScriptRoot "cloud-init.yaml")
        vm "cloud-init status --wait >/dev/null"
    } elseif ($row.State -eq "Stopped") {
        Invoke-Multipass start $VM
    }
    Install-CorpCa
}

function Install-CorpCa {
    # The office network re-signs Docker Hub with the company CA (TLS inspection).
    # Give the VM the same trust Windows already has: export the CA's public
    # certificate and add it to the VM's store (docker restarts only if it changed).
    $subject = if ($env:DEVNODE_CA_SUBJECT) { $env:DEVNODE_CA_SUBJECT } else { "CN=devoogt-CA" }
    $cert = Get-ChildItem Cert:\LocalMachine\Root | Where-Object { $_.Subject -match [regex]::Escape($subject) } | Select-Object -First 1
    if (-not $cert) { Write-Host "No '$subject' CA in the Windows root store - skipping CA install"; return }
    $pem = Join-Path $env:TEMP "devnode-corp-ca.crt"
    $b64 = [Convert]::ToBase64String($cert.RawData, [Base64FormattingOptions]::InsertLineBreaks)
    [IO.File]::WriteAllText($pem, "-----BEGIN CERTIFICATE-----`n$($b64 -replace "`r", '')`n-----END CERTIFICATE-----`n")
    Invoke-Multipass transfer $pem "${VM}:/tmp/devnode-corp-ca.crt"
    vm ("cmp -s /tmp/devnode-corp-ca.crt /usr/local/share/ca-certificates/devnode-corp-ca.crt || " +
        "{ sudo install -m 644 /tmp/devnode-corp-ca.crt /usr/local/share/ca-certificates/devnode-corp-ca.crt " +
        "&& sudo update-ca-certificates && sudo systemctl restart docker; }")
}

function Sync-Repo {
    vm "sudo rm -rf $Remote/module $Remote/devnode $Remote/grafana && mkdir -p $Remote"   # tools container leaves root-owned files
    foreach ($dir in "module", "devnode") {
        Invoke-Multipass transfer --recursive (Join-Path $Repo $dir) "${VM}:$Remote/"
    }
    vm "find $Remote -name __pycache__ -prune -exec rm -rf {} +"
}

function Copy-Back([string]$name) {
    # `multipass transfer` to Windows exits 2 ("cannot set permissions") even when
    # the copy worked, and capturing `multipass exec cat` output can spin forever -
    # so transfer to a local temp file, ignore the exit code, and check the file.
    $tmp = Join-Path $env:TEMP "devnode-copyback-$name"
    Remove-Item $tmp -ErrorAction SilentlyContinue
    & multipass transfer "${VM}:$Remote/module/$name" $tmp 2>$null
    if (-not (Test-Path $tmp) -or (Get-Item $tmp).Length -eq 0) { throw "could not copy $name back from the VM" }
    Copy-Item $tmp (Join-Path $Repo "module\$name") -Force
}

function Test-Module {
    compose "--profile tools run --rm tools"
    foreach ($f in "poetry.lock", "Trending_pb2.py", "Trending_pb2_grpc.py") { Copy-Back $f }
    Write-Host "poetry.lock and the gRPC stubs copied back to module\"
}

function Show-Endpoints {
    $ip = ((& multipass info $VM --format json | ConvertFrom-Json).info.$VM.ipv4)[0]
    Write-Host ""
    Write-Host "Dev node is up at $ip"
    Write-Host "  Portal health : http://${ip}:8080/api/v1/edge-module-request/devnode/module/datalogger-extractor/health"
    Write-Host "  CSVs          : .\devnode\devnode.ps1 files"
}

switch ($Command) {
    "up"    { Ensure-Vm; Sync-Repo; Test-Module; compose "up -d --build --remove-orphans"; Show-Endpoints }
    "sync"  { Ensure-Vm; Sync-Repo; compose "up -d --build --remove-orphans"; Show-Endpoints }
    "test"  { Ensure-Vm; Sync-Repo; Test-Module }
    "logs"  { compose "logs -f extractor" }
    "files" { vm "cd $Remote/devnode && tr -d '\r' < show-output.sh > /tmp/so.sh && bash /tmp/so.sh extractor-data && bash /tmp/so.sh real-data" }
    "real"  {
        Ensure-Vm; Sync-Repo
        # DataLoggerGRPC.exe runs on this Windows PC: from the VM that is its default gateway
        # (the Hyper-V switch), unless devnode\.env sets DATALOGGER_ADDR.
        $gw = ((& multipass exec $VM '--' bash -lc "ip route show default") -split '\s+')[2]
        vm ("cd $Remote/devnode && touch .env && if ! grep -q '^DATALOGGER_ADDR=' .env; then echo DATALOGGER_ADDR=${gw}:50715 >> .env; fi " +
            "&& grep '^DATALOGGER_ADDR=' .env && (nc -z -w 5 `$(grep '^DATALOGGER_ADDR=' .env | cut -d= -f2 | tr ':' ' ') " +
            "&& echo 'port reachable' || echo 'PORT NOT REACHABLE - is DataLoggerGRPC.exe running, and does Windows Firewall allow it?')")
        compose "--profile real run --rm --build real"
        vm "cd $Remote/devnode && tr -d '\r' < show-output.sh > /tmp/so.sh && bash /tmp/so.sh real-data"
    }
    "shell" { & multipass shell $VM }
    "down"  { compose "down" }
}
