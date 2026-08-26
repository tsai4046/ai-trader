# 策略研究彙整（sub-agent 網路調查，2026-08）

三個研究方向的調查結果摘要：趨勢/動能、均值回歸/拉回、波動壓縮/量能。
篩選約束：**long-only、日線、單一標的 OHLCV 可計算**（不需基本面、不需跨市場排名）。
入選為 detector 的策略在 `core/signals.py`；全部**預設不啟用**——先跑
`python run.py evaluate` 用你自己的 watchlist 多輪驗證，通過門檻再加進
`config.yaml` 的 `signals.enabled_detectors`。

## 已實作為 detector 的 6 個

| detector | 來源 | 核心規則 | 證據等級 |
|---|---|---|---|
| `rsi2_pullback` | Connors & Alvarez,《Short Term Trading Strategies That Work》(2008) | close>SMA200 且 RSI(2)<5（Wilder） | 出版書 + 多方獨立複測（SPY 勝率 ~75-91%） |
| `double_seven` | 同上 | close>SMA200 且收盤為 7 日新低 | 出版書 + QuantifiedStrategies/Alvarez 複測 |
| `week52_breakout` | George & Hwang, *Journal of Finance* 59(5), 2004 | 收盤突破前 252 日高點 | 同儕審查論文（原文為橫斷面排名，此為單檔改編） |
| `donchian55_breakout` | 海龜 System 2（Faith,《Way of the Turtle》, 2007） | 收盤突破前 55 日高點 | 公開完整規則 + 實績（1980s，已知衰退） |
| `adx_trend` | Wilder,《New Concepts in Technical Trading Systems》(1978) | +DI 上穿 −DI 且 ADX(14)>25 | 原典定義；獨立回測顯示適合當濾網多於單獨進場 |
| `squeeze_breakout` | Bollinger,《Bollinger on Bollinger Bands》(2001) | BB(20,2) 帶寬創 125 日新低（近 5 根內）後收盤突破上軌 | 出版書規則明確；績效表視市場而異 |

實作上的刻意偏離（也記於 NOTES.md）：

- **Connors 原版不設停損**（他的數據顯示停損反而傷績效）。本系統一律套 ATR 停損 +
  RR 目標的統一回測引擎——所以文獻上的勝率數字不能直接對照，我們自己的 evaluate 才算數。
- Connors 系列的門檻是在**美股指數 ETF** 上校準的；用在個股波動更大、跳空風險更高。
- `week52_breakout` 用預設 `lookback_days: 500`（約 345 根 K）時樣本會偏少；
  想認真評估它可把 `data.lookback_days` 調到 900+。

## 調查過但未實作的（與原因）

**訊號頻率太低，過不了「樣本不足上限 45 分」這關**（月線級系統，單檔 500 根 K 只有 1-3 次訊號）：
- 12 個月時間序列動能（Moskowitz-Ooi-Pedersen, *JFE* 2012）——證據最強但頻率最低
- Faber 10 月均線擇時（*JWM* 2007）——本質是減回撤工具
- 黃金交叉 50/200（60 年回測樣本也只有 33 筆）

**需要跨標的排名或多資產資料**（超出單檔 OHLCV 約束）：
- Jegadeesh-Titman 橫斷面動能、IBD RS 評分（Minervini 條件 8）
- Antonacci 雙動能 GEM（需三個資產序列 + 國庫券利率）

**本質主觀、無穩定量化版本**：
- Minervini VCP（收縮幾何靠人眼；量化 proxy 只有短窗口單一多頭市場的回測）
- OBV / A-D 背離（連 QuantifiedStrategies 都說「很難回測」）

**證據太弱**：
- Keltner 上軌突破單獨使用（S&P 500 回測 CAGR 僅 4.7%）
- Chaikin oscillator 零軸交叉（CAGR ~2.4%）

**同家族重複**（與已實作者高度重疊，避免假的「多重確認」）：
- Cumulative RSI、R3、RSI(4)<25、%b 策略、3-Day High/Low、MDU —— 都是
  Connors RSI 家族；`rsi2_pullback` 的子條件已納入 cumulative RSI 的持續性檢查
- ID/NR4、NR7、TTM Squeeze —— 壓縮家族，已由 `squeeze_breakout` 代表

## 通用警語（研究一致的結論）

1. 壓縮類 setup（squeeze/NR7）**本身不含方向資訊**——多頭邊只拿得到一半樣本，
   且 Bollinger 本人警告第一次突破常是假方向（head fake）。
2. 均值回歸在**崩盤時失效最嚴重**——訊號會在最糟的時候密集出現，SMA200 濾網
   是唯一內建防線（本系統另有六道風控閘）。
3. 趨勢類共同弱點：盤整期 whipsaw、進出場皆偏晚、V 型反轉吃虧、
   發表後因擁擠而衰退。
4. 文獻績效多為**指數/ETF、無成本、分散組合**的數字；單檔個股 + 實際成本
   必然更差。這正是每檔自己回測（原則 2）而不是抄文獻分數的理由。

（完整調查含全部公式、參數、出處連結，見本次對話的三份 sub-agent 報告；
本檔為決策摘要。）
