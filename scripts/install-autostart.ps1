#Requires -Version 5.1
<#
.SYNOPSIS
Starts spylead at Windows logon, or stops doing so with -Remove (SPEC.md section 15).

.DESCRIPTION
Writes a launcher to %LOCALAPPDATA%\spylead\launch.ps1, on the system drive, and registers a per-user logon task
named spylead that runs it. The launcher waits up to 10 minutes for the project's venv, because the project lives
on an external drive, then starts .venv\Scripts\pythonw.exe -m spylead --config config.toml in the project folder.

If registering the task is refused, a shortcut in the user's Startup folder runs the launcher instead.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Remove
#>
[CmdletBinding()]
param(
    [switch]$Remove
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$TaskName = 'spylead'
$Project = Split-Path -Parent $PSScriptRoot
$LauncherFolder = Join-Path $env:LOCALAPPDATA 'spylead'
$Launcher = Join-Path $LauncherFolder 'launch.ps1'
$Shortcut = Join-Path ([Environment]::GetFolderPath('Startup')) 'spylead.lnk'
$PowerShell = Join-Path $PSHOME 'powershell.exe'
# The task and the shortcut run the same command. Bypass applies to this one process only.
$Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Launcher`""

function Get-LauncherText {
    $text = @'
# Written by scripts\install-autostart.ps1 in the spylead project. Run it again after moving the project.
# The project lives on an external drive: wait up to 10 minutes for its venv, then start spylead hidden.
$Project = '__PROJECT__'
$Python = Join-Path $Project '.venv\Scripts\pythonw.exe'
$Log = Join-Path $PSScriptRoot 'launch.log'
$Deadline = (Get-Date).AddMinutes(10)
while (-not (Test-Path -LiteralPath $Python)) {
    if ((Get-Date) -ge $Deadline) {
        Add-Content -LiteralPath $Log -Value ((Get-Date -Format s) + ' gave up: ' + $Python + ' did not appear within 10 minutes')
        exit 1
    }
    Start-Sleep -Seconds 5
}
$Arguments = @('-m', 'spylead', '--config', 'config.toml')
$Process = Start-Process -FilePath $Python -ArgumentList $Arguments -WorkingDirectory $Project -WindowStyle Hidden -PassThru
Add-Content -LiteralPath $Log -Value ((Get-Date -Format s) + ' started spylead, process ' + $Process.Id)
'@
    # The path goes into a single-quoted string, where a quote is written twice.
    return $text.Replace('__PROJECT__', $Project.Replace("'", "''"))
}

function Register-SpyleadTask {
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action = New-ScheduledTaskAction -Execute $PowerShell -Argument $Arguments
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    # The defaults would skip a logon on battery power.
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $task = @{
        TaskName = $TaskName
        Action = $action
        Trigger = $trigger
        Principal = $principal
        Settings = $settings
        Description = 'Starts spylead at logon'
        Force = $true
    }
    Register-ScheduledTask @task | Out-Null
}

function New-StartupShortcut {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($Shortcut)
    $link.TargetPath = $PowerShell
    $link.Arguments = $Arguments
    $link.WorkingDirectory = $LauncherFolder
    $link.WindowStyle = 7  # minimized
    $link.Description = 'Starts spylead at logon'
    $link.Save()
}

function Install-Autostart {
    New-Item -ItemType Directory -Force -Path $LauncherFolder | Out-Null
    Set-Content -LiteralPath $Launcher -Value (Get-LauncherText) -Encoding UTF8
    try {
        Register-SpyleadTask
    } catch {
        New-StartupShortcut
        Write-Host "Registering the logon task was refused: $($_.Exception.Message)"
        Write-Host "Instead, a shortcut in the Startup folder starts spylead at logon: $Shortcut"
        return
    }
    # A shortcut left by an earlier fallback would start spylead a second time.
    if (Test-Path -LiteralPath $Shortcut) {
        Remove-Item -LiteralPath $Shortcut
    }
    Write-Host "The logon task '$TaskName' starts spylead from $Project at each logon, through $Launcher."
}

function Remove-Autostart {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed the logon task '$TaskName'."
    }
    if (Test-Path -LiteralPath $Shortcut) {
        Remove-Item -LiteralPath $Shortcut
        Write-Host "Removed the Startup shortcut $Shortcut."
    }
    if (Test-Path -LiteralPath $LauncherFolder) {
        Remove-Item -LiteralPath $LauncherFolder -Recurse
        Write-Host "Removed $LauncherFolder."
    }
}

if ($Remove) {
    Remove-Autostart
} else {
    Install-Autostart
}
