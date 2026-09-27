# 本脚本已由同目录 create_shortcut.py 取代（WorkBuddy 沙箱拦 PowerShell COM，
# 且此处写死了旧目录 D:\运动综合数据平台 的绝对路径）。仅作存档保留，勿再执行。
$ws = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop '运动综合数据平台.lnk'
if (Test-Path $lnk) { Remove-Item $lnk -Force }
$sc = $ws.CreateShortcut($lnk)
$sc.TargetPath = 'D:\运动综合数据平台\backend\.venv\Scripts\pythonw.exe'
$sc.Arguments = '"D:\运动综合数据平台\desktop\desktop.py"'
$sc.WorkingDirectory = 'D:\运动综合数据平台\backend'
$sc.IconLocation = 'D:\运动综合数据平台\desktop\app.ico'
$sc.Description = '运动综合数据平台 - 训练 · 评估 · 预测'
$sc.Save()
Write-Output ('created: ' + $lnk)
