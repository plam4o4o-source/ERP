# Одит (06.10.2026): проверка на прехода PachoLogistic → PHLogistics на
# истински Windows (release.yml, ПРЕДИ публикуването — провал спира изданието).
#
#   -Scenario setup : стара инсталация с данни + новият инсталатор (/VERYSILENT),
#                     после новото .exe — данните трябва да са преместени.
#   -Scenario cli   : старото .exe (= компилираното PHLogistics.exe, копирано като
#                     PachoLogistic.exe в старата папка) с
#                     --complete-rename-with <инсталатор> — същият път като бутона
#                     на таблото; скриптът за прехода пуска инсталатора и новото .exe.
#   -Scenario portable : (одит 07.10.2026) преносима папка (C:\phl_portable) с
#                     PachoLogistic.exe (= компилираното PHLogistics.exe) и данни
#                     със старите имена — при старта всичко се преименува НА МЯСТО.
#   -Scenario shared   : същата папка, но споделена (New-SmbShare) — НИЩО не се
#                     преименува, отчетът казва „shared“; делът се маха накрая.
#
# Проверките са в scripts/ci_rename_fixture.py (истинска SQLite база с маркер).
param(
    [Parameter(Mandatory = $true)][ValidateSet("setup", "cli", "portable", "shared")][string]$Scenario,
    [string]$Setup = "dist_installer\PHLogistics-Setup.exe",
    [string]$Exe = "dist\PHLogistics.exe"
)
$ErrorActionPreference = "Stop"
$Setup = (Resolve-Path $Setup).Path
$Exe = (Resolve-Path $Exe).Path
$Fixture = (Resolve-Path "scripts\ci_rename_fixture.py").Path
$Programs = Join-Path $env:LOCALAPPDATA "Programs"
$NewDir = Join-Path $Programs "PHLogistics"
$LegacyDir = Join-Path $Programs "PachoLogistic"
$SetupLog = Join-Path $env:RUNNER_TEMP "ph_migration_setup_$Scenario.log"
# Без автоматично обновяване от GitHub по време на проверката (виж
# updater.start_auto_update_loop); наследява се и от скрипта за прехода.
# Одит (07.10.2026): новото име; старото остава — .exe-то под старо име
# (PachoLogistic.exe) трябва да спазва забраната и ако е стара версия.
$env:PH_DISABLE_AUTO_UPDATE = "1"
$env:PACHO_DISABLE_AUTO_UPDATE = "1"
# Преносимите сценарии: папка извън %LOCALAPPDATA%\Programs и име на дяла.
$PortableDir = if ($Scenario -eq "shared") { "C:\phl_shared" } else { "C:\phl_portable" }
$ShareName = "phltest"

function Stop-App {
    Get-Process -Name "PHLogistics", "PachoLogistic" -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
}

function Show-Folder([string]$Path) {
    Write-Host "--- $Path ---"
    if (Test-Path $Path) {
        Get-ChildItem -Force -Recurse $Path -ErrorAction SilentlyContinue |
            ForEach-Object { Write-Host ("{0}  {1}" -f $_.FullName, $_.Length) }
    } else {
        Write-Host "(няма такава папка)"
    }
}

function Show-File([string]$Path) {
    if (Test-Path $Path) {
        Write-Host "--- $Path ---"
        Get-Content $Path -Tail 200 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host $_ }
    }
}

function Remove-TestShare {
    if ($Scenario -eq "shared") {
        Remove-SmbShare -Name $ShareName -Force -ErrorAction SilentlyContinue
    }
}

function Show-Diagnostics {
    Write-Host "--- processes (cmd/ping/tasklist/PHLogistics/PachoLogistic/setup) ---"
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^(cmd|ping|tasklist|find|PHLogistics|PachoLogistic|PHLogistics-Setup|PHLogistics-Setup\.tmp)' -or $_.Name -like '*.tmp' } |
        ForEach-Object { Write-Host ("{0} pid={1} ppid={2} {3}" -f $_.Name, $_.ProcessId, $_.ParentProcessId, $_.CommandLine) }
    Show-Folder $NewDir
    Show-Folder $LegacyDir
    Show-File (Join-Path $NewDir "ph_migration.json")
    if ($Scenario -in @("portable", "shared")) {
        Show-Folder $PortableDir
        Show-File (Join-Path $PortableDir "ph_migration.json")
        Write-Host "--- SMB shares ---"
        Get-SmbShare -ErrorAction SilentlyContinue | ForEach-Object { Write-Host ("{0} -> {1}" -f $_.Name, $_.Path) }
        Write-Host "--- HKLM LanmanServer\Shares ---"
        $key = "HKLM:\SYSTEM\CurrentControlSet\Services\LanmanServer\Shares"
        if (Test-Path $key) {
            (Get-Item $key).GetValueNames() | ForEach-Object {
                Write-Host ("{0}: {1}" -f $_, ((Get-ItemProperty $key).$_ -join " | "))
            }
        } else { Write-Host "(no key)" }
    }
    foreach ($dir in @($NewDir, $LegacyDir, $PortableDir)) {
        Get-ChildItem -Path $dir -Filter "ph_startup*.log" -ErrorAction SilentlyContinue |
            ForEach-Object { Show-File $_.FullName }
    }
    Show-File $SetupLog
    Get-ChildItem -Path $env:TEMP -Directory -Filter "ph_rename_*" -ErrorAction SilentlyContinue |
        ForEach-Object {
            Show-Folder $_.FullName
            Get-ChildItem -Path $_.FullName -Filter "*.log" -ErrorAction SilentlyContinue |
                ForEach-Object { Show-File $_.FullName }
        }
}

function Fail([string]$Message) {
    Write-Host "FAILED ($Scenario): $Message"
    Show-Diagnostics
    Stop-App
    Remove-TestShare
    exit 1
}

# Start-Process -Wait чака и ВСИЧКИ наследници (job object) — в сценария „cli“
# това е и откаченият скрипт, и новото .exe, което не излиза. Чакаме само
# самия процес; `.Handle` се взима веднага, иначе ExitCode остава празен.
function Invoke-AndWait([string]$File, [string[]]$Arguments, [int]$Seconds) {
    $proc = Start-Process -FilePath $File -ArgumentList $Arguments -PassThru
    $null = $proc.Handle
    if (-not $proc.WaitForExit($Seconds * 1000)) { Fail "$File did not finish within $Seconds s" }
    return $proc.ExitCode
}

function Wait-Login([int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "http://127.0.0.1:5000/login"
            if ($resp.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Seconds 2
    }
    return $false
}

# --- чисто начало: нищо работещо, нито новата, нито старата папка
Stop-App
try {
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://127.0.0.1:5000/login" | Out-Null
    Fail "port 5000 is still answering before the test (a previous exe is running)"
} catch { }
Remove-Item -Recurse -Force $NewDir, $LegacyDir -ErrorAction SilentlyContinue
Get-ChildItem -Path $env:TEMP -Directory -Filter "ph_rename_*" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# Одит (07.10.2026): преносима инсталация — без инсталатор, данните до .exe-то.
if ($Scenario -in @("portable", "shared")) {
    Remove-TestShare
    Remove-Item -Recurse -Force $PortableDir -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force $PortableDir | Out-Null
    python $Fixture create-portable $PortableDir
    if ($LASTEXITCODE -ne 0) { Fail "could not create the fake portable install" }
    $portableExe = Join-Path $PortableDir "PachoLogistic.exe"
    Copy-Item $Exe $portableExe
    if ($Scenario -eq "shared") {
        # Runner-ът е администратор; без услугата „Server“ няма дялове.
        Start-Service -Name LanmanServer -ErrorAction SilentlyContinue
        try {
            New-SmbShare -Name $ShareName -Path $PortableDir -FullAccess "Everyone" -ErrorAction Stop | Out-Null
        } catch {
            Fail "New-SmbShare failed: $($_.Exception.Message)"
        }
        $shareKey = "HKLM:\SYSTEM\CurrentControlSet\Services\LanmanServer\Shares"
        $value = (Get-ItemProperty -Path $shareKey -Name $ShareName -ErrorAction SilentlyContinue).$ShareName
        if (-not ($value -match [regex]::Escape("Path=$PortableDir"))) {
            Fail "the share is not in LanmanServer\Shares (the program reads it there): $value"
        }
    }
    Start-Process -FilePath $portableExe -WorkingDirectory $PortableDir | Out-Null
    if (-not (Wait-Login 120)) { Fail "the portable exe did not serve /login within 120 s" }
    Stop-App
    $check = if ($Scenario -eq "shared") { "check-shared" } else { "check-portable" }
    python $Fixture $check $PortableDir
    if ($LASTEXITCODE -ne 0) { Fail "$check failed" }
    Show-File (Join-Path $PortableDir "ph_migration.json")
    Write-Host "Migration scenario '$Scenario' - OK"
    Remove-TestShare
    Remove-Item -Recurse -Force $PortableDir -ErrorAction SilentlyContinue
    exit 0
}

New-Item -ItemType Directory -Force $LegacyDir | Out-Null
python $Fixture create $LegacyDir
if ($LASTEXITCODE -ne 0) { Fail "could not create the fake legacy install" }

if ($Scenario -eq "setup") {
    Set-Content -Path (Join-Path $LegacyDir "PachoLogistic.exe") -Value "MZ fake legacy exe" -Encoding ascii
    $code = Invoke-AndWait $Setup @(
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER", "/LOG=`"$SetupLog`"") 300
    if ($code -ne 0) { Fail "setup exited with code $code" }
    $newExe = Join-Path $NewDir "PHLogistics.exe"
    if (-not (Test-Path $newExe)) { Fail "setup did not place PHLogistics.exe in $NewDir" }
    Start-Process -FilePath $newExe | Out-Null
    if (-not (Wait-Login 120)) { Fail "the new exe did not serve /login within 120 s" }
} else {
    $legacyExe = Join-Path $LegacyDir "PachoLogistic.exe"
    Copy-Item $Exe $legacyExe
    $code = Invoke-AndWait $legacyExe @("--complete-rename-with", "`"$Setup`"") 120
    if ($code -ne 0) { Fail "--complete-rename-with exited with code $code" }
    if (-not (Wait-Login 300)) { Fail "the new exe did not serve /login within 300 s" }
    # Скриптът пише OK, щом новата версия потвърди старта (updater.confirm_started).
    $deadline = (Get-Date).AddSeconds(90)
    $ok = $false
    while ((Get-Date) -lt $deadline -and -not $ok) {
        $log = Get-ChildItem -Path $env:TEMP -Directory -Filter "ph_rename_*" -ErrorAction SilentlyContinue |
            ForEach-Object { Join-Path $_.FullName "ph_rename.log" } | Where-Object { Test-Path $_ } |
            Select-Object -First 1
        if ($log -and (Select-String -Path $log -Pattern "^OK" -Quiet)) { $ok = $true } else { Start-Sleep -Seconds 2 }
    }
    if (-not $ok) { Fail "the rename script did not report OK" }
}

Stop-App
python $Fixture check $NewDir $LegacyDir
if ($LASTEXITCODE -ne 0) { Fail "migration check failed" }
Show-File (Join-Path $NewDir "ph_migration.json")
Write-Host "Migration scenario '$Scenario' - OK"
# Оставяме машината чиста за следващите стъпки.
Remove-Item -Recurse -Force $NewDir, $LegacyDir -ErrorAction SilentlyContinue
exit 0
