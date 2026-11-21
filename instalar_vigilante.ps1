# instalar_vigilante.ps1
# Registra en Windows una tarea que arranca actualizador.py en segundo plano (sin ventana) cada vez que
# ESTE usuario inicia sesión, y lo reinicia solo si se cae. Revisa el repositorio cada N minutos.
#
#   Instalar / reinstalar:   powershell -ExecutionPolicy Bypass -File .\instalar_vigilante.ps1
#   Otro intervalo:          powershell -ExecutionPolicy Bypass -File .\instalar_vigilante.ps1 -CadaMinutos 5
#   Ver estado:              powershell -ExecutionPolicy Bypass -File .\instalar_vigilante.ps1 -Estado
#   Quitar:                  powershell -ExecutionPolicy Bypass -File .\instalar_vigilante.ps1 -Quitar
#
# No reinicia Movimientos y no toca config.py: el vigilante solo trae cambios del repo y los anota en actualizador.log.

param(
    [switch]$Quitar,
    [switch]$Estado,
    [int]$CadaMinutos = 10
)

$ErrorActionPreference = 'Stop'
$nombre  = 'Movimientos-Actualizador'
$carpeta = $PSScriptRoot
$script  = Join-Path $carpeta 'actualizador.py'
$usuario = "$env:USERDOMAIN\$env:USERNAME"

function Detener-Vigilante {
    # Para solo el pythonw que corre ESTE actualizador (no otros python).
    Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*actualizador.py*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

if ($Estado) {
    $t = Get-ScheduledTask -TaskName $nombre -ErrorAction SilentlyContinue
    if (-not $t) { Write-Host "La tarea '$nombre' NO está instalada."; exit 0 }
    $i = Get-ScheduledTaskInfo -TaskName $nombre
    Write-Host "Tarea '$nombre': estado $($t.State). Último arranque: $($i.LastRunTime). Resultado: $($i.LastTaskResult)."
    $vivo = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*actualizador.py*' }
    Write-Host ("Proceso en segundo plano: " + $(if ($vivo) { "SÍ (pid $($vivo.ProcessId))" } else { "NO" }))
    $log = Join-Path $carpeta 'actualizador.log'
    if (Test-Path $log) { Write-Host "--- últimas líneas de actualizador.log ---"; Get-Content $log -Tail 8 }
    exit 0
}

if ($Quitar) {
    Unregister-ScheduledTask -TaskName $nombre -Confirm:$false -ErrorAction SilentlyContinue
    Detener-Vigilante
    Write-Host "Tarea '$nombre' eliminada y vigilante detenido."
    exit 0
}

if (-not (Test-Path $script)) { throw "No encuentro actualizador.py en $carpeta" }
if ($CadaMinutos -lt 1) { throw "-CadaMinutos debe ser 1 o más." }

# pythonw.exe = python sin ventana de consola
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (& py -c "import sys;print(sys.executable)" 2>$null) }
if (-not $py) { throw "No encuentro Python en el PATH." }
$pythonw = Join-Path (Split-Path $py) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { throw "No encuentro pythonw.exe junto a $py" }

# git debe estar disponible (el script también lo busca en C:\Program Files\Git si no está en el PATH)
if (-not (Get-Command git -ErrorAction SilentlyContinue) -and -not (Test-Path "$env:ProgramFiles\Git\cmd\git.exe")) {
    Write-Warning "No encuentro git. Instálalo (https://git-scm.com/download/win) o el vigilante no podrá actualizar."
}

$accion = New-ScheduledTaskAction -Execute $pythonw `
    -Argument "`"$script`" --cada $CadaMinutos" -WorkingDirectory $carpeta
$disparador = New-ScheduledTaskTrigger -AtLogOn -User $usuario
$config = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $usuario -LogonType Interactive -RunLevel Limited

Detener-Vigilante
Register-ScheduledTask -TaskName $nombre -Action $accion -Trigger $disparador -Settings $config `
    -Principal $principal -Description "Trae al PC los cambios del repo Bancolombia-Movimientos (no reinicia nada)." -Force | Out-Null
Start-ScheduledTask -TaskName $nombre

Write-Host "Listo. Tarea '$nombre' instalada para ${usuario}:"
Write-Host "  - arranca sola al iniciar sesión, sin ventana, y se reinicia si se cae"
Write-Host "  - revisa el repo cada $CadaMinutos min; el registro queda en $carpeta\actualizador.log"
Write-Host "  - ya la inicié ahora mismo. Comprueba con:  .\instalar_vigilante.ps1 -Estado"
