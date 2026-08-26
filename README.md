# 全天候 AI 交易員（決策支援，不自動下單）

持續掃描市場 → 辨識訊號 → 回測驗證 → 制定計畫 → 六道硬性風控閘 → 決策備忘錄 → **人工核准**。

輸出三種對外狀態：`APPROVE`（等人按執行）/ `WATCH`（繼續監控）/ `REJECT`（不做），
另有內部第四態 `ABSTAIN`（資料不足 / 資料源故障 / 訊號衝突），對外併入 REJECT 但標記
`needs_human: true`，在儀表板獨立區塊顯示。**系統不接券商、不自動下單。**

設計原則（詳見 `ai-trader-spec` §2，程式碼與測試都守著這幾條）：

1. 規則歸程式，LLM 歸文字 — 型態判定全部是 pandas 確定性條件
2. 沒有回測就沒有分數 — 樣本 < `min_samples` 時分數上限 45（最多 WATCH）
3. 風控是程式碼，不是提示詞 — `core/risk.py` 不 import 定性層
4. LLM 只有否決權，沒有升級權 — `apply_llm_verdict` + 測試守住
5. 便宜的先過濾，貴的後呼叫 — LLM 每輪最多 `max_candidates` 次
6. 不確定就停下來 — 缺資料 / 衝突 → ABSTAIN + needs_human

---

## 安裝

```bash
./setup.sh        # macOS / Linux 一鍵：建 venv、裝依賴、跑測試、離線驗證一輪
```

Windows（PowerShell）：

```powershell
.\setup.ps1       # 同上；結尾會印出工作排程器（schtasks）的排程指令
```

或手動：`python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && python -m pytest`

## FinMind token 設定（台股）

到 <https://finmindtrade.com> 註冊取得 token（免費額度即可跑 watchlist 規模），
**只放環境變數，不要寫進 config**：

```bash
export FINMIND_TOKEN="你的token"
```

沒有 token 也能呼叫，但額度很低，容易 429 → 該檔會 ABSTAIN。

## 跑第一輪

```bash
python run.py scan --offline    # 先用合成資料離線驗證整條管線
python run.py scan              # 真實資料全市場（us + tw watchlist）
python run.py scan --market us  # 只掃美股
python run.py backtest --symbol NVDA   # 單獨看某檔各 detector 的 edge 統計
```

結束時 stdout 會印出 `X 核准 / Y 觀察 / Z 拒絕 / W 需人工確認`，
並在 `out/memo_YYYYMMDD_HHMM.html` 產出備忘錄。

## 看懂儀表板

- **頂部狀態列**：淨值、總曝險（對上限的進度條）、當前回撤、全域狀態。
  回撤超限或當日虧損超限時整條變紅 = 觀察模式，本輪所有候選一律降為 WATCH。
- **三欄卡片**（核准 / 觀察 / 拒絕）：
  - 分數下的堆疊條拆成 **edge（藍）與 setup（綠）** 兩段——一眼看出分數是回測撐起來的
    還是型態撐起來的。edge 段短、setup 段長的高分要特別小心。
  - 回測小表：`n < 15` 或 `holdout 衰退` 會出現橘色警告，這種訊號分數被硬性壓在 45。
  - Sparkline 上三組線：綠色帶 = 進場區間、紅虛線 = 停損、綠虛線 = 目標。
  - **失效條件（紫色區塊）≠ 停損**：它是「當初進場邏輯不成立了」的提前出場條件，
    通常早於停損發生，兩者分開看。
  - 六道閘 chips：✗ 的會顯示實際值 vs 上限。任何一閘 ✗（全域回撤閘除外）= REJECT。
- **需要人工確認（ABSTAIN）**：資料不足 / 訊號衝突 / 資料源故障，系統拒絕硬做決定的清單。
- **持倉與計畫監控**：OPEN 部位距停損多遠、已持有 vs 預計天數；待執行計畫是否已失效/過期。

## 策略評估（找出對你的清單真正有效的策略）

系統內建 11 個 detector：規格原生 5 個 + 研究引入 6 個（Connors RSI-2、Double 7s、
52 週高點突破、海龜 55 日突破、ADX 趨勢、Bollinger squeeze——出處與取捨見
`docs/strategy-research.md`）。研究引入的**預設全部不啟用**。

```bash
python run.py evaluate            # 全部 detector × 你的 watchlist，多輪驗證
```

每個 detector×標的會做 walk-forward 時間分段（預設 4 段）驗證，「推薦」需同時滿足：
樣本數達門檻、整體期望值為正、多數時間分段為正、holdout 未衰退——四者缺一即淘汰。
結果印在終端機、寫入 `out/evaluate_*.html`，儀表板底部也會顯示排行。

要啟用通過驗證的策略，手動編輯 `config.yaml`：

```yaml
signals:
  enabled_detectors: [breakout, pullback, momentum, trend_continuation, donchian55_breakout]
```

系統**不會**自動啟用任何策略——評估是淘汰工具，最後一步永遠是人。
評估不做參數搜尋（那是過擬合的捷徑）；`week52_breakout` 這類長回看策略
建議把 `data.lookback_days` 調到 900 以上再評估。

## 本機管理台（最簡單的日常操作方式）

```bash
python run.py serve        # 自動開瀏覽器 http://127.0.0.1:8787/
```

瀏覽器介面可以做四件事，不用記任何指令、不用手改檔案：

1. **加入 / 移除追蹤股票**——輸入代號即可，台股（數字）美股（字母）自動判別，寫入 `watchlist.yaml`
2. **登記庫存**——既有持倉（不是系統訊號買的也可以）填進場價、股數、停損即納入
   風控計算：總曝險（Gate 2）、相關性（Gate 3）、儀表板持倉監控區全部生效。停損必填。
3. **一鍵平倉**——自動算 R 與損益寫入 `data/outcomes.jsonl`
4. **跑一輪掃描**——按鈕觸發，完成後直接開最新備忘錄

只綁 127.0.0.1、無帳號驗證，僅供本機單人使用，不要開放到區網或公網。

## 回填 outcomes（系統長期價值所在）

決策後把實際結果記回來，`run_id` 在儀表板底部與 journal 內：

```bash
# 真的進場了
python run.py open  --run-id 20260820T0836Z-2673 --entry-price 1188

# 平倉（自動算 R 與損益）
python run.py close --run-id 20260820T0836Z-2673 --exit-price 1310 \
                    --exit-date 2026-09-02 --reason target

# 系統核准但你沒做——一定要記，累積後才能回答「我否決 AI 的那些次是對是錯」
python run.py skip  --run-id 20260820T0836Z-2673 --notes "法說會前不進場"

python run.py stats     # 實際勝率 / 平均 R / 權益曲線回撤
python run.py monitor   # 只檢查舊計畫失效/過期，不找新訊號
```

`OPEN` 持倉會回饋給風控閘（總曝險、相關性、回撤全部從 `data/outcomes.jsonl` 重建）。

## cron 排程（v1 就是本機 cron + HTML）

```cron
# 美股收盤後（台北時間 05:30，週二～週六）
30 5 * * 2-6  cd /path/to/ai-trader && ./.venv/bin/python run.py scan --market us >> data/cron.log 2>&1
# 台股收盤後（台北時間 14:30，週一～週五）
30 14 * * 1-5 cd /path/to/ai-trader && ./.venv/bin/python run.py scan --market tw >> data/cron.log 2>&1
```

## 這套系統做不到什麼（誠實條款）

- 回測是**單標的、無交易成本、無滑價**的簡化模擬，實際績效必然更差。
- 樣本數再多也不保證未來有效；`decayed` 標記只是最基本的衰退偵測。
- 台股籌碼面（三大法人、融資券）v1 未納入訊號判定。
- 沒有處理財報、除權息、股票分割等事件風險；FinMind 台股價格未還原權值，
  除權息會造成假跌破（見 `NOTES.md`）。
- 台美股混用時的匯率風險未計入曝險計算。
- **這不是投資建議，最終決策與後果都在人身上。**

## 專案結構

```
run.py            CLI（scan / backtest / monitor / open / close / skip / stats）
config.yaml       所有可調參數（程式碼零魔術數字）
watchlist.yaml    自選清單（us / tw）
core/
  config.py       pydantic 驗證，缺欄位直接退出
  datasource.py   yfinance / FinMind / Synthetic 三個 provider + 快取
  indicators.py   純函式指標（Wilder ATR/RSI、不含當根的 donchian/swing）
  signals.py      五個 detector + 評分（樣本不足上限 45）
  backtest.py     隔日開盤進場、同根雙觸及算停損、holdout 衰退偵測
  plan.py         進場區間 / 停損 / 目標 / 失效條件（RR 用最差進場價）
  risk.py         六道硬性閘（不接受任何 LLM 輸入）
  llm.py          定性層（可選、只有否決權）
  journal.py      append-only JSONL + jsonschema + 組合重建
  report.py       單檔自包含 HTML 儀表板（斷網可開）
data/journal.jsonl   每次決策一筆（append-only）
data/outcomes.jsonl  人工回填的實際結果
out/memo_*.html      決策備忘錄
```
