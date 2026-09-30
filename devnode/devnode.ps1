<#
Dev edge node for the DataLogger extractor, in a Multipass VM (Windows host).

  .\devnode\devnode.ps1 up      create the VM if needed, copy the repo in, lock + stubs + tests, start the stack
  .\devnode\devnode.ps1 sync    copy module/ and devnode/ in again and rebuild
  .\devnode\devnode.ps1 test    poetry lock + proto stubs + pytest in the VM; copies poetry.lock and stubs back
  .\devnode\devnode.ps1 logs    follow the extractor's logs
  .\devnode\devnode.ps1 hdc     what the fake Helin Data Collector received
  .\devnode\devnode.ps1 outage-real  real DataLogger, one past day, with the Cloud HDC link cut: onboard DB gets it all, cloud catches up after
  .\devnode\devnode.ps1 outage  cut the Cloud HDC link for 3 min: onboard DB + Grafana must carry on, cloud must catch up
  .\devnode\devnode.ps1 files   list CSVs from a real run with --dest-type csv
  .\devnode\devnode.ps1 real    run the container once against the real DataLoggerGRPC.exe
                                (settings: DATALOGGER_ADDR, REAL_START, REAL_END, REAL_RATE in devnode\.env)
  .\devnode\devnode.ps1 shell   open a shell in the VM
  .\devnode\devnode.ps1 down    stop the stack (VM keeps running)
#>
param([ValidateSet("up", "sync", "test", "logs", "hdc", "outage", "outage-real", "files", "real", "shell", "down")][string]$Command = "up")

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
    foreach ($dir in "module", "devnode", "grafana") {
        Invoke-Multipass transfer --recursive (Join-Path $Repo $dir) "${VM}:$Remote/"
    }
    vm "find $Remote -name __pycache__ -prune -exec rm -rf {} +"
    # What the node gets from its host folder /var/lib/helin/config/datalogger, and a
    # self-signed certificate for the fake HDC (the real one is self-signed too).
    vm ("cd $Remote/devnode && mkdir -p config certs && cp ../module/signals/bn716_tags_subset.txt config/tags " +
        "&& openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj /CN=HelinDataCollector " +
        "-keyout certs/hdc-key.pem -out certs/hdc-cert.pem 2>/dev/null && chmod 644 certs/*.pem")
    # Test tags matching the local DataLoggerGRPC.exe (bnXXX) replace the bn716 list when present.
    $tags = if ($env:DEVNODE_TAGS) { $env:DEVNODE_TAGS } else { "I:\bnXXX\export\profiles\tags.csv" }
    if (Test-Path $tags) {
        Invoke-Multipass transfer $tags "${VM}:$Remote/devnode/config/tags"
        Write-Host "config/tags <- $tags"
    }
}

function Copy-Back([string]$name) {
    # `multipass transfer` to Windows exits 2 ("cannot set permissions") even when
    # the copy worked, and capturing `multipass exec cat` output can spin forever -
    # so transfer to a local temp file, ignore the exit code, and check the file.
    $tmp = Join-Path $env:TEMP "devnode-copyback-$name"
    Remove-Item $tmp -ErrorAction SilentlyContinue
    $ErrorActionPreference = "Continue"   # PS 5.1 turns native stderr into a terminating error under Stop
    & multipass transfer "${VM}:$Remote/module/$name" $tmp 2>$null
    $ErrorActionPreference = "Stop"
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
    Write-Host "  Fake HDC      : .\devnode\devnode.ps1 hdc   (what the extractor delivered, per north output)"
    Write-Host "  Grafana       : http://${ip}:3000  (TimescaleDB replica; anonymous view, admin / helin-dev to edit)"
}

function Show-Hdc {
    vm "python3 $Remote/devnode/show-hdc.py"
}

switch ($Command) {
    "up"    { Ensure-Vm; Sync-Repo; Test-Module; compose "up -d --build --remove-orphans"; Show-Endpoints }
    "sync"  { Ensure-Vm; Sync-Repo; compose "up -d --build --remove-orphans --force-recreate"; Show-Endpoints }
    "test"  { Ensure-Vm; Sync-Repo; Test-Module }
    "logs"  { compose "logs -f extractor" }
    "hdc"   { Show-Hdc }
    "outage-real" { vm "cd $Remote/devnode && tr -d '\r' < outage-real.sh > /tmp/outage-real.sh && bash /tmp/outage-real.sh" }
    "outage" { vm "cd $Remote/devnode && tr -d '\r' < outage-test.sh > /tmp/outage.sh && bash /tmp/outage.sh 180" }
    "files" { vm "cd $Remote/devnode && tr -d '\r' < show-output.sh > /tmp/so.sh && bash /tmp/so.sh real-data" }
    "real"  {
        Ensure-Vm; Sync-Repo
        # DataLoggerGRPC.exe runs on this Windows PC. Use an SSH reverse tunnel on the
        # VM's 127.0.0.1:50715 when one is up (Windows Firewall blocks the VM otherwise),
        # else the VM's default gateway (the Hyper-V switch). $env:DATALOGGER_ADDR overrides.
        $addr = $env:DATALOGGER_ADDR
        if (-not $addr) {
            $gw = ((& multipass exec $VM '--' bash -lc "ip route show default") -split '\s+')[2]
            & multipass exec $VM '--' bash -lc "nc -z -w 2 127.0.0.1 50715 2>/dev/null"
            $addr = if ($LASTEXITCODE -eq 0) { "127.0.0.1:50715" } else { "${gw}:50715" }
        }
        Write-Host "DataLogger: $addr"
        vm ("cd $Remote/devnode && sed -i '/^DATALOGGER_ADDR=/d' .env 2>/dev/null; echo DATALOGGER_ADDR=$addr >> .env " +
            "&& (nc -z -w 5 $($addr -replace ':', ' ') && echo 'port reachable' || " +
            "echo 'PORT NOT REACHABLE - is DataLoggerGRPC.exe running? Open the SSH tunnel or allow it in Windows Firewall')")
        compose "--profile extractor up -d --build --force-recreate extractor"
        Write-Host "Extractor started against the real DataLogger; follow it with .\devnode\devnode.ps1 logs"
    }
    "shell" { & multipass shell $VM }
    "down"  { compose "down" }
}