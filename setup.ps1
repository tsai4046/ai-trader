# 本地部署（Windows PowerShell）：建 venv、裝依賴、跑測試、離線驗證一輪。
# 若被執行原則擋下，先跑：Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# 找 Python（優先 py launcher）
$python = $null
if (Get-Command py -ErrorAction SilentlyContinue) { $python = "py"; $pyArgs = @("-3") }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $python = "python"; $pyArgs = @() }
else { Write-Error "找不到 Python，請先安裝 3.11+（https://www.python.org/downloads/，記得勾 Add to PATH）" }

Write-Host "==> 建立虛擬環境 .venv"
& $python @pyArgs -m venv .venv
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

Write-Host "==> 安裝依賴"
& $venvPy -m pip install --quiet --upgrade pip
& $venvPy -m pip install --quiet -r requirements.txt

Write-Host "==> 跑全套測試"
& $venvPy -m pytest -q
if ($LASTEXITCODE -ne 0) { Write-Error "測試未全數通過，請把輸出貼回來排查" }

Write-Host "==> 離線驗證一輪（synthetic provider）"
& $venvPy run.py scan --offline
if ($LASTEXITCODE -ne 0) { Write-Error "離線掃描失敗" }

$root = $PSScriptRoot
Write-Host @"

✔ 部署完成。接下來：

  1) 台股資料源 token（免費註冊 https://finmindtrade.com）——設成使用者環境變數，
     工作排程器的任務也讀得到：
       setx FINMIND_TOKEN "你的token"
     （setx 只影響之後開的視窗；目前這個視窗要再補 `$env:FINMIND_TOKEN = "你的token"）

  2) 跑真實資料：
       & "$root\.venv\Scripts\python.exe" run.py scan
       Invoke-Item (Get-ChildItem out\memo_*.html | Sort-Object LastWriteTime | Select-Object -Last 1)

  3) 排程（工作排程器，時間為台北 UTC+8；用「系統管理員」PowerShell 執行）：
       schtasks /Create /TN "ai-trader-us" /SC WEEKLY /D TUE,WED,THU,FRI,SAT /ST 05:30 ``
         /TR "\"$root\.venv\Scripts\python.exe\" \"$root\run.py\" scan --market us"
       schtasks /Create /TN "ai-trader-tw" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 14:30 ``
         /TR "\"$root\.venv\Scripts\python.exe\" \"$root\run.py\" scan --market tw"
     之後想移除：schtasks /Delete /TN "ai-trader-us" /F（tw 同理）
"@
