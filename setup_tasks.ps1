# setup_tasks.ps1
# Run as Administrator:
#   cd E:\Book\Scanner
#   powershell -ExecutionPolicy Bypass -File setup_tasks.ps1

$PYTHON = "C:\Users\njubr\AppData\Local\Programs\Python\Python314\python.exe"

function Register-PyTask {
    param (
        [string]$TaskName,
        [string]$ScriptPath,
        [string]$WorkingDir,
        [object]$Trigger,
        [string]$Description
    )
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "  Removed existing: $TaskName"
    }
    $action = New-ScheduledTaskAction -Execute $PYTHON -Argument "`"$ScriptPath`"" -WorkingDirectory $WorkingDir
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 2) -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 10) -StartWhenAvailable -RunOnlyIfNetworkAvailable
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $Trigger -Settings $settings -Principal $principal -Description $Description | Out-Null
    Write-Host "  Registered: $TaskName"
}

Write-Host ""
Write-Host "Task 1: BooksGoat Weekly Scraper..."
$scraperTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At "06:00AM"
Register-PyTask -TaskName "BooksGoat Weekly Scraper" -ScriptPath "E:\Book\Scraper\booksgoat_scraper.py" -WorkingDir "E:\Book\Scraper" -Trigger $scraperTrigger -Description "Scrapes BooksGoat sources. Pushes CSV to GitHub for 9AM scanner."

Write-Host ""
Write-Host "Task 2: BooksGoat Daily Tracker..."
$trackerTrigger = New-ScheduledTaskTrigger -Daily -At "07:00AM"
Register-PyTask -TaskName "BooksGoat Daily Tracker" -ScriptPath "E:\Book\Tracker\booksgoat_tracker.py" -WorkingDir "E:\Book\Tracker" -Trigger $trackerTrigger -Description "OOS and price checks. Delists via eBay API. Pushes CSV to GitHub."

Write-Host ""
Write-Host "Done. Verify:"
$tasks = @("BooksGoat Weekly Scraper", "BooksGoat Daily Tracker")
foreach ($t in $tasks) {
    $task = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    if ($task) {
        $info = Get-ScheduledTaskInfo -TaskName $t
        $nextRun = if ($info.NextRunTime) { $info.NextRunTime.ToString("yyyy-MM-dd HH:mm") } else { "N/A" }
        Write-Host ("  [OK]  {0,-35} Next run: {1}" -f $t, $nextRun)
    } else {
        Write-Host "  [FAIL] $t not found"
    }
}
Write-Host ""
Write-Host "Test manually:"
Write-Host "  Start-ScheduledTask -TaskName BooksGoat_Weekly_Scraper"
Write-Host "  Start-ScheduledTask -TaskName BooksGoat_Daily_Tracker"
