"""可插拔資料源。統一輸出 Bars；任何抓取失敗都回傳空 df 的 Bars，不 raise 中斷整輪。

絕對不做前視填補：不 ffill 未來值、不用未來資料補洞。
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Protocol

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

CACHE_DIR = Path("data/cache")
RETRY_BACKOFF_SECONDS = [1.0, 4.0]        # §6：重試 2 次（退避 1s, 4s）
FINMIND_MIN_CALL_INTERVAL = 0.3           # §5.1：FinMind rate limit
FINMIND_API_URL = "https://api.finmindtrade.com/api/v4/data"

REQUIRED_COLUMNS = ["open", "high", "low", "close", "volume"]


@dataclass
class Bars:
    symbol: str
    market: str                # 'us' | 'tw'
    df: pd.DataFrame           # index=DatetimeIndex(tz-naive), columns=REQUIRED_COLUMNS (float)
    source: str
    fetched_at: datetime
    stale_days: int

    @property
    def empty(self) -> bool:
        return self.df is None or self.df.empty


class Provider(Protocol):
    def fetch(self, symbol: str, lookback_days: int, interval: str) -> Bars: ...


def _stale_days(df: pd.DataFrame, today: date | None = None) -> int:
    """今天 − 最後一根 K 的日期，扣除週末（以工作日計）。"""
    if df is None or df.empty:
        return 10**6
    today = today or date.today()
    last = df.index[-1].date()
    if last >= today:
        return 0
    return int(np.busday_count(last, today))


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """統一欄位為小寫 float、tz-naive DatetimeIndex、依日期排序去重。"""
    df = df.copy()
    df.columns = [str(c).lower() for c in df.columns]
    df = df[[c for c in REQUIRED_COLUMNS if c in df.columns]]
    if list(df.columns) != REQUIRED_COLUMNS:
        missing = set(REQUIRED_COLUMNS) - set(df.columns)
        raise ValueError(f"缺少必要欄位: {sorted(missing)}")
    df = df.astype(float)
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    df.index = idx.normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def _empty_bars(symbol: str, market: str, source: str) -> Bars:
    df = pd.DataFrame(columns=REQUIRED_COLUMNS, index=pd.DatetimeIndex([]))
    return Bars(symbol=symbol, market=market, df=df, source=source,
                fetched_at=datetime.now(timezone.utc), stale_days=10**6)


def _cache_path(market: str, symbol: str, interval: str) -> Path:
    return CACHE_DIR / f"{market}_{symbol}_{interval}.parquet"


def _read_cache(market: str, symbol: str, interval: str, ttl_hours: float) -> pd.DataFrame | None:
    p = _cache_path(market, symbol, interval)
    if not p.exists():
        return None
    age_hours = (time.time() - p.stat().st_mtime) / 3600.0
    if age_hours > ttl_hours:
        return None
    try:
        return pd.read_parquet(p)
    except Exception:
        return None


def _write_cache(market: str, symbol: str, interval: str, df: pd.DataFrame) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_parquet(_cache_path(market, symbol, interval))
    except Exception as e:  # 快取失敗不致命
        log.warning("寫入快取失敗 %s: %s", symbol, e)


def _fetch_with_retry(fn, symbol: str):
    """重試 2 次（1s, 4s 退避）。全失敗 → raise 最後一個例外給呼叫端轉空 Bars。"""
    last_exc: Exception | None = None
    for attempt in range(1 + len(RETRY_BACKOFF_SECONDS)):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if attempt < len(RETRY_BACKOFF_SECONDS):
                time.sleep(RETRY_BACKOFF_SECONDS[attempt])
    raise last_exc  # type: ignore[misc]


class YFinanceProvider:
    """美股。auto_adjust=True；新版 yfinance 回傳 MultiIndex column，要攤平。"""

    source = "yfinance"
    market = "us"

    def __init__(self, cfg):
        self.cfg = cfg

    def fetch(self, symbol: str, lookback_days: int, interval: str) -> Bars:
        cached = _read_cache(self.market, symbol, interval, self.cfg.data.cache_ttl_hours)
        if cached is not None and not cached.empty:
            return Bars(symbol, self.market, cached, self.source,
                        datetime.now(timezone.utc), _stale_days(cached))
        try:
            df = _fetch_with_retry(lambda: self._download(symbol, lookback_days, interval), symbol)
        except Exception as e:
            log.error("yfinance 抓取失敗 %s: %s", symbol, e)
            return _empty_bars(symbol, self.market, self.source)
        if df.empty:
            return _empty_bars(symbol, self.market, self.source)
        _write_cache(self.market, symbol, interval, df)
        return Bars(symbol, self.market, df, self.source,
                    datetime.now(timezone.utc), _stale_days(df))

    def _download(self, symbol: str, lookback_days: int, interval: str) -> pd.DataFrame:
        import yfinance as yf

        yf_interval = {"1d": "1d", "60m": "60m"}[interval]
        raw = yf.download(symbol, period=f"{lookback_days}d", interval=yf_interval,
                          auto_adjust=True, progress=False)
        if raw is None or raw.empty:
            return pd.DataFrame(columns=REQUIRED_COLUMNS, index=pd.DatetimeIndex([]))
        if isinstance(raw.columns, pd.MultiIndex):   # 攤平 MultiIndex（陷阱 #2）
            raw.columns = raw.columns.get_level_values(0)
        return _normalize(raw)


class FinMindProvider:
    """台股。TaiwanStockPrice 為未還原股價（限制記於 NOTES.md）。"""

    source = "finmind"
    market = "tw"
    _last_call_ts: float = 0.0

    def __init__(self, cfg):
        self.cfg = cfg
        self.token = os.environ.get(cfg.markets.tw.finmind_token_env, "")

    def fetch(self, symbol: str, lookback_days: int, interval: str) -> Bars:
        cached = _read_cache(self.market, symbol, interval, self.cfg.data.cache_ttl_hours)
        if cached is not None and not cached.empty:
            return Bars(symbol, self.market, cached, self.source,
                        datetime.now(timezone.utc), _stale_days(cached))
        try:
            df = _fetch_with_retry(lambda: self._download(symbol, lookback_days), symbol)
        except Exception as e:
            log.error("finmind 抓取失敗 %s: %s", symbol, e)
            return _empty_bars(symbol, self.market, self.source)
        if df.empty:
            return _empty_bars(symbol, self.market, self.source)
        _write_cache(self.market, symbol, interval, df)
        return Bars(symbol, self.market, df, self.source,
                    datetime.now(timezone.utc), _stale_days(df))

    def _download(self, symbol: str, lookback_days: int) -> pd.DataFrame:
        import requests

        # rate limit：每次呼叫間隔 >= 0.3 秒（class 層級共用）
        elapsed = time.time() - FinMindProvider._last_call_ts
        if elapsed < FINMIND_MIN_CALL_INTERVAL:
            time.sleep(FINMIND_MIN_CALL_INTERVAL - elapsed)
        FinMindProvider._last_call_ts = time.time()

        start = (pd.Timestamp.today() - pd.Timedelta(days=int(lookback_days * 1.6))).date()
        params = {
            "dataset": "TaiwanStockPrice",
            "data_id": symbol,
            "start_date": str(start),
        }
        if self.token:
            params["token"] = self.token
        resp = requests.get(FINMIND_API_URL, params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        rows = payload.get("data") or []
        if not rows:
            return pd.DataFrame(columns=REQUIRED_COLUMNS, index=pd.DatetimeIndex([]))
        raw = pd.DataFrame(rows)
        raw = raw.rename(columns={
            "open": "open", "max": "high", "min": "low",
            "close": "close", "Trading_Volume": "volume",
        })
        raw.index = pd.DatetimeIndex(pd.to_datetime(raw["date"]))
        return _normalize(raw[REQUIRED_COLUMNS])


# --- SyntheticProvider：CI 與離線驗證的骨幹 -----------------------------------

SYN_DEFAULT_BARS = 500
SYN_SHORT_BARS = 100
SYN_BASE_VOLUME = 1_000_000.0

# --offline 模式的 watchlist：情境代號取代真實標的
OFFLINE_UNIVERSE = [
    {"symbol": "SYN_TREND",    "sector": "syn_a", "name": "SYN_TREND",    "market": "us"},
    {"symbol": "SYN_BREAK",    "sector": "syn_a", "name": "SYN_BREAK",    "market": "us"},
    {"symbol": "SYN_PULLBACK", "sector": "syn_b", "name": "SYN_PULLBACK", "market": "us"},
    {"symbol": "SYN_FLAT",     "sector": "syn_b", "name": "SYN_FLAT",     "market": "tw"},
    {"symbol": "SYN_CRASH",    "sector": "syn_c", "name": "SYN_CRASH",    "market": "tw"},
    {"symbol": "SYN_SHORT",    "sector": "syn_c", "name": "SYN_SHORT",    "market": "tw"},
]


def _stable_seed(symbol: str) -> int:
    """同一 symbol 每次（跨 process）結果必須完全相同。
    Python 內建 hash() 對 str 有隨機鹽，不符合決定性要求 → 改用 md5（記於 NOTES.md）。
    """
    return int(hashlib.md5(symbol.encode("utf-8")).hexdigest(), 16) % 2**32


class SyntheticProvider:
    """離線測試用。固定 seed 的幾何布朗運動 + 依 symbol 注入可控情境。"""

    source = "synthetic"

    def __init__(self, cfg=None, market: str = "us"):
        self.cfg = cfg
        self.market = market

    def fetch(self, symbol: str, lookback_days: int, interval: str) -> Bars:
        n = SYN_SHORT_BARS if symbol == "SYN_SHORT" else max(SYN_DEFAULT_BARS, 1)
        df = self._generate(symbol, n)
        return Bars(symbol=symbol, market=self.market, df=df, source=self.source,
                    fetched_at=datetime.now(timezone.utc), stale_days=_stale_days(df))

    # -- 情境產生 --------------------------------------------------------------

    def _generate(self, symbol: str, n: int) -> pd.DataFrame:
        rng = np.random.default_rng(_stable_seed(symbol))
        t = np.arange(n)

        if symbol == "SYN_TREND":
            close, volume = self._trend(rng, n)
        elif symbol == "SYN_BREAK":
            close, volume = self._breakout(rng, n)
        elif symbol == "SYN_PULLBACK":
            close, volume = self._pullback(rng, n)
        elif symbol == "SYN_FLAT":
            close, volume = self._flat(n)
        elif symbol == "SYN_CRASH":
            close, volume = self._crash(rng, n)
        else:  # SYN_SHORT 與其他任意代號：一般 GBM
            ret = rng.normal(0.0002, 0.015, n)
            close = 100.0 * np.exp(np.cumsum(ret))
            volume = SYN_BASE_VOLUME * (1.0 + 0.2 * np.abs(np.sin(t / 7.0)))

        # 由 close 構造 OHLC：日內振幅與收盤變動成比例，保證 high>=max(o,c)、low<=min(o,c)
        open_ = np.empty(n)
        open_[0] = close[0]
        open_[1:] = close[:-1]
        spread = np.maximum(np.abs(close - open_) * 0.3, close * 0.003)
        high = np.maximum(open_, close) + spread
        low = np.minimum(open_, close) - spread

        idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n)
        return pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
            index=idx,
        )

    @staticmethod
    def _trend(rng, n):
        """穩定上升趨勢：trend_continuation 必觸發（sma20>sma50>sma200、close>sma20）。"""
        ret = 0.0012 + rng.normal(0.0, 0.003, n)
        ret[-3:] = 0.004   # 尾端確定性收高，保證 close > sma20
        close = 100.0 * np.exp(np.cumsum(ret))
        volume = SYN_BASE_VOLUME * (1.0 + 0.1 * np.sin(np.arange(n) / 9.0))
        return close, volume

    @staticmethod
    def _breakout(rng, n):
        """階梯式：緩漲建立多頭 → 盤整箱體 → 帶量突破。每 ~45 根一次突破，
        歷史上重複多次（給回測樣本），最後一根正好是帶量突破 → breakout 必觸發。
        """
        cycle = 45
        box_len = 30
        close = np.empty(n)
        volume = np.full(n, SYN_BASE_VOLUME)
        level = 100.0
        # 前 200 根：緩漲建立 sma50 > sma200
        warmup = 200
        ret = 0.0009 + rng.normal(0.0, 0.002, warmup)
        close[:warmup] = level * np.exp(np.cumsum(ret))
        level = close[warmup - 1]
        i = warmup
        while i < n:
            box_end = min(i + box_len, n)
            # 盤整：在 level 的 ±1.2% 內震盪，且不創新高
            for j in range(i, box_end):
                close[j] = level * (1.0 + 0.012 * math.sin((j - i) / 4.0) - 0.002)
                volume[j] = SYN_BASE_VOLUME * (0.9 + 0.1 * math.sin(j / 5.0))
            i = box_end
            rally_end = min(i + (cycle - box_len), n)
            for k, j in enumerate(range(i, rally_end)):
                # 突破當根跳 3% 且量能 3 倍，其後續漲
                close[j] = close[j - 1] * (1.03 if k == 0 else 1.008)
                volume[j] = SYN_BASE_VOLUME * (3.0 if k == 0 else 1.4)
            i = rally_end
            if i > 0:
                level = close[i - 1]
        # 確保最後一根就是突破根：把序列裁到最後一次盤整後的第一根突破
        # 上面的迴圈以 cycle 為週期，n=500、warmup=200 → (500-200)=300 = 45*6+30
        # 最後 30 根是盤整、不足以放 rally → 手動把最後一根改成帶量突破
        box_high = close[-21:-1].max()
        close[-1] = box_high * 1.03
        volume[-1] = SYN_BASE_VOLUME * 3.0
        return close, volume

    @staticmethod
    def _pullback(rng, n):
        """上升趨勢 + 週期性回檔至 SMA20：最後一根正好回到均線附近 → pullback 必觸發。"""
        close = np.empty(n)
        close[0] = 100.0
        cycle_up, cycle_down = 15, 6
        i = 1
        up = True
        while i < n:
            seg = cycle_up if up else cycle_down
            for j in range(i, min(i + seg, n)):
                drift = 0.006 if up else -0.007
                close[j] = close[j - 1] * (1.0 + drift + rng.normal(0.0, 0.001))
            i += seg
            up = not up
        # 讓結尾落在回檔段的均線附近：往回找最近一次 down 段結束點裁切位置不可行
        # （長度固定），改為把最後 4 根做成溫和回檔到 SMA20 附近
        sma20_prev = close[-24:-4].mean()
        for k in range(4):
            target = sma20_prev * (1.0 + 0.002 * (3 - k) / 3.0)
            close[-4 + k] = target
        volume = SYN_BASE_VOLUME * (1.0 + 0.1 * np.sin(np.arange(n) / 8.0))
        return close, volume

    @staticmethod
    def _flat(n):
        """無方向盤整：微幅下漂 + 衰減震盪，所有 detector 皆不得觸發。
        量能固定 → breakout 的量能條件恆 False；衰減震盪 → momentum 不進前 20%。
        """
        t = np.arange(n)
        drift = (1.0 - 0.0001) ** t
        wobble = 1.0 + 0.012 * np.sin(2 * np.pi * t / 37.0) * (0.999 ** t)
        close = 100.0 * drift * wobble
        volume = np.full(n, SYN_BASE_VOLUME)
        return close, volume

    @staticmethod
    def _crash(rng, n):
        """前段平穩、尾段高波動下跌：測 ATR spike 與回撤閘。"""
        calm = n - 15
        ret = rng.normal(0.0003, 0.004, calm)
        close_calm = 100.0 * np.exp(np.cumsum(ret))
        close_crash = [close_calm[-1]]
        for k in range(15):
            shock = -0.05 if k % 2 == 0 else 0.02   # 大跌大反彈交錯 → TR 爆量
            close_crash.append(close_crash[-1] * (1.0 + shock))
        close = np.concatenate([close_calm, np.array(close_crash[1:])])
        volume = np.concatenate([
            np.full(calm, SYN_BASE_VOLUME),
            np.full(15, SYN_BASE_VOLUME * 2.5),
        ])
        return close, volume


def get_provider(name: str, cfg) -> Provider:
    if name == "yfinance":
        return YFinanceProvider(cfg)
    if name == "finmind":
        return FinMindProvider(cfg)
    if name == "synthetic":
        return SyntheticProvider(cfg)
    raise ValueError(f"未知的 provider: {name}")


def load_universe(cfg, offline: bool = False, market: str | None = None) -> list[dict]:
    """合併 us + tw watchlist。offline 模式改用 SYN_* 情境代號。"""
    if offline:
        entries = OFFLINE_UNIVERSE
    else:
        from core.config import load_watchlist

        wl = load_watchlist(cfg.markets.us.symbols_from)
        entries = []
        if cfg.markets.us.enabled:
            entries += wl["us"]
        if cfg.markets.tw.enabled:
            entries += wl["tw"]
    if market:
        entries = [e for e in entries if e["market"] == market]
    return entries
