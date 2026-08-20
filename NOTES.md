# NOTES — 規格外的取捨紀錄

實作過程中規格沒寫到、或必須偏離字面的決定，全部記在這裡（依規格前言要求，一律選最保守做法）。

## 資料源

1. **Synthetic seed 不用內建 `hash()`**。規格寫 `seed = hash(symbol) % 2**32`，但 Python 對 str 的
   `hash()` 有 per-process 隨機鹽（PYTHONHASHSEED），不符「同一 symbol 每次結果必須完全相同」的硬性要求。
   改用 `md5(symbol) % 2**32`，跨程序、跨機器皆決定性。
2. **SyntheticProvider 不寫 parquet 快取**：產生成本為零且完全決定性，寫快取反而會讓跨日測試讀到
   舊日期索引。`--offline` 模式同時停用 edge 統計快取，理由相同。
3. **FinMind `TaiwanStockPrice` 是未還原股價**（陷阱 #3）：除權息會造成假跌破。v1 記錄此限制而不改用
   還原股價 dataset（需另外的權限/方案）。使用台股訊號時要自行留意除權息日。
4. **lookback 轉換**：`lookback_days` 以交易日理解，抓取時乘 1.6 轉 calendar days 以涵蓋週末與假日。
5. **本開發環境的 egress proxy 擋掉 Yahoo 與 FinMind（CONNECT 403）**：真實 provider 的欄位對應、
   MultiIndex 攤平、重試、快取邏輯全部以 mock 測試驗證（`tests/test_datasource.py`）；
   並實測過 `scan --market us` 在資料源全掛時正確逐檔 ABSTAIN、不中斷整輪。

## 回測

6. **距資料尾端不足 `horizon_bars` 的訊號不納入樣本**：規格未定義部分持有期的處理，計入會引入
   「最近訊號永遠提早結算」的偏差，最保守是直接剔除。
7. **holdout 為空（樣本太少切不出）時 `decayed = False`**：`holdout_expectancy` 記 0.0。
   樣本不足的懲罰由「分數上限 45」承擔，不重複懲罰。

## 訊號

8. **衍生指標在暖身期的 NaN 視為「條件不成立」**，不 fillna；原始 OHLCV 出現 NaN 才整檔 ABSTAIN
   （`data_gaps`）。註：momentum 的 250 日分位數需要約 270 根 K，資料在 250~270 根之間時
   該子條件為 False（不觸發），而不是 ABSTAIN——否則 `min_bars_required: 250` 形同 270。
9. **reversal 的權重規格未給**（只說 fired = 全部成立）：取 0.4 / 0.3 / 0.3。

## 計畫

10. **reversal 的進場區間與失效條件規格未定義**（預設關閉的 detector）：進場比照 breakout
    `[close, close + 0.3*ATR]`；失效條件取保守的「連續 2 根收盤 < SMA20」。
11. **`stop_too_wide` 的 entry 以最差進場價 `entry_high` 衡量**：距離較大、較容易觸發 REJECT，較保守。
12. **pullback 的「回檔起算 swing low」**以 `swing_low(20)`（不含當根）計。

## 風控

13. **drawdown 閘與其他閘同時失敗時以 REJECT 為準**；只有 drawdown 一閘失敗（全域觀察模式）才依規格
    降為 WATCH。
14. **相關性閘的資料不足判定**：兩序列報酬重疊 < 30 根即視為算不出，退回 sector 計數 fallback，
    detail 中註明哪些持倉用了 fallback。

## Journal / CLI

15. **無訊號的標的不寫 journal**：規格說「一次決策一筆」，對沒有任何 detector 觸發的標的並沒有決策
    可記，每輪灌入幾十筆無資訊紀錄會稀釋 journal 的長期價值。ABSTAIN 與所有有觸發的標的一定寫。
16. **補了 `open` / `skip` 兩個最小子指令**：規格只要求 `close`，但 outcomes 的 `OPEN` / `SKIPPED`
    狀態需要有入口才能成立（Gate 2/3/4 都依賴 OPEN 持倉重建）。僅做最小欄位。
17. **監控紀錄沿用原 run_id** 寫入 `INVALIDATED` / `EXPIRED`：同一 run_id 的最新一筆代表該計畫的
    目前狀態，方便重建。
18. **匯率**：台美混市時曝險計算不換匯（config 的 `currency` 只是計價標記），限制已寫進 README。
