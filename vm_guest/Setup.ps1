# Run this script as Administrator INSIDE the logged-in Windows VM.
$ErrorActionPreference = 'Stop'
$vmInstallRoot = 'C:\QQBotVM'
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run Setup.ps1 as Administrator inside the virtual machine.'
}
New-Item -ItemType Directory -Path $vmInstallRoot -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'agent.py') -Destination (Join-Path $vmInstallRoot 'agent.py')
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'guest.json') -Destination (Join-Path $vmInstallRoot 'guest.json')
$vmPythonRoot = Join-Path $vmInstallRoot 'python'
if (-not (Test-Path -LiteralPath (Join-Path $vmPythonRoot 'python.exe'))) {
    Expand-Archive -LiteralPath (Join-Path $PSScriptRoot 'python.zip') -DestinationPath $vmPythonRoot
}
if (-not (Get-NetFirewallRule -DisplayName 'QQBot isolated VM bridge' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName 'QQBot isolated VM bridge' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 47653 -RemoteAddress '10.0.2.2' | Out-Null
}
$vmUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$vmAction = New-ScheduledTaskAction -Execute (Join-Path $vmPythonRoot 'pythonw.exe') -Argument ('"' + (Join-Path $vmInstallRoot 'agent.py') + '"') -WorkingDirectory $vmInstallRoot
$vmTrigger = New-ScheduledTaskTrigger -AtLogOn -User $vmUser
$vmPrincipal = New-ScheduledTaskPrincipal -UserId $vmUser -LogonType Interactive -RunLevel Highest
$vmSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'QQBotVMBridge' -Action $vmAction -Trigger $vmTrigger -Principal $vmPrincipal -Settings $vmSettings -Force | Out-Null
Start-ScheduledTask -TaskName 'QQBotVMBridge'
Write-Output 'Guest bridge installed. Keep this VM user logged in; the host console can now launch authorized games.'
