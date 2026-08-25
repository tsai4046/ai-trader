# 本地部署（Windows PowerShell）：建 venv、裝依賴、跑測試、離線驗證一輪。
# 若被執行原則擋下，先跑：Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# 找 Python（優先 py launcher）。注意：Windows 內建的 python.exe 可能是
# Microsoft Store 的假捷徑，執行 --version 會失敗，要驗證後才算數。
$installHint = "請先安裝 Python 3.11+：winget install Python.Python.3.12，" +
               "或到 https://www.python.org/downloads/ 下載（勾選 Add python.exe to PATH），裝完重開 PowerShell"
$python = $null
foreach ($cand in @(@("py", @("-3")), @("python", @()))) {
    if (Get-Command $cand[0] -ErrorAction SilentlyContinue) {
        $ver = & $cand[0] @($cand[1]) --version 2>&1
        if ($LASTEXITCODE -eq 0 -and "$ver" -match "^Python 3") {
            $python = $cand[0]; $pyArgs = $cand[1]
            Write-Host "==> 使用 $ver"
            break
        }
    }
}
if (-not $python) { Write-Error "找不到可用的 Python（PATH 上的可能是 Microsoft Store 假捷徑）。$installHint" }

Write-Host "==> 建立虛擬環境 .venv"
& $python @pyArgs -m venv .venv
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { Write-Error "虛擬環境建立失敗（$venvPy 不存在）。$installHint" }

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
