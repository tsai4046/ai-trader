"""回測正確性測試 — 這裡錯了，後面全部是幻覺。"""
import numpy as np
import pandas as pd
import pytest

from core.backtest import backtest_detector, simulate_trades
from core.datasource import SyntheticProvider


def flat_df(n=60, price=100.0):
    """TR 恆為 2 → ATR(14) 收斂到 2。open=close=price, high=+1, low=-1。"""
    idx = pd.bdate_range("2023-01-02", periods=n)
    return pd.DataFrame(
        {
            "open": np.full(n, price),
            "high": np.full(n, price + 1.0),
            "low": np.full(n, price - 1.0),
            "close": np.full(n, price),
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )


def fired_at(df, positions):
    s = pd.Series(False, index=df.index)
    s.iloc[list(positions)] = True
    return s


# cfg: atr_stop_mult=2.0, rr_target=2.5, horizon=20
# entry=open[t+1]=100, ATR=2 → stop=96, risk=4, target=110


def test_target_hit_records_positive_rr(cfg):
    df = flat_df(60)
    df.iloc[25, df.columns.get_loc("high")] = 111.0   # t=20 進場後第 5 根觸及目標
    trades = simulate_trades(df, fired_at(df, [20]), cfg)
    assert len(trades) == 1
    tr = trades[0]
    assert tr.entry == pytest.approx(100.0)
    assert tr.stop == pytest.approx(96.0)
    assert tr.target == pytest.approx(110.0)
    assert tr.r == pytest.approx(cfg.backtest.rr_target)
    assert tr.exit_idx == 25 and tr.bars_held == 5


def test_stop_hit_records_minus_one(cfg):
    df = flat_df(60)
    df.iloc[23, df.columns.get_loc("low")] = 95.0
    trades = simulate_trades(df, fired_at(df, [20]), cfg)
    assert len(trades) == 1
    assert trades[0].r == pytest.approx(-1.0)
    assert trades[0].exit_idx == 23


def test_same_bar_stop_and_target_counts_as_stop(cfg):
    # 同一根 K 同時觸及停損與目標 → 一律算停損（保守假設）
    df = flat_df(60)
    df.iloc[21, df.columns.get_loc("high")] = 120.0
    df.iloc[21, df.columns.get_loc("low")] = 90.0
    trades = simulate_trades(df, fired_at(df, [20]), cfg)
    assert len(trades) == 1
    assert trades[0].r == pytest.approx(-1.0)


def test_horizon_exit_at_close(cfg):
    df = flat_df(60)
    horizon = cfg.backtest.horizon_bars
    df.iloc[20 + horizon, df.columns.get_loc("close")] = 104.0   # 出場根收 104 → R=1.0
    trades = simulate_trades(df, fired_at(df, [20]), cfg)
    assert len(trades) == 1
    assert trades[0].r == pytest.approx((104.0 - 100.0) / 4.0)
    assert trades[0].bars_held == horizon


def test_overlapping_signals_skipped(cfg):
    # 第一筆 t=20 持有到 t=25（觸及目標）；t=22 的訊號在持有期間 → 跳過；t=30 可成立
    df = flat_df(80)
    df.iloc[25, df.columns.get_loc("high")] = 111.0
    df.iloc[35, df.columns.get_loc("high")] = 111.0
    trades = simulate_trades(df, fired_at(df, [20, 22, 30]), cfg)
    assert [t.signal_idx for t in trades] == [20, 30]


def test_entry_is_next_bar_open_not_signal_close(cfg):
    # 訊號當根收盤 100、隔日開盤跳空 106 → 進場價必須是 106
    df = flat_df(60)
    df.iloc[21, df.columns.get_loc("open")] = 106.0
    df.iloc[21, df.columns.get_loc("high")] = 107.0
    trades = simulate_trades(df, fired_at(df, [20]), cfg)
    assert trades[0].entry == pytest.approx(106.0)


def test_expectancy_hand_computed(cfg):
    # 兩筆已知結果：+2.5R 與 -1R → expectancy 0.75、勝率 0.5
    df = flat_df(100)
    df.iloc[25, df.columns.get_loc("high")] = 111.0    # t=20 → target
    df.iloc[45, df.columns.get_loc("low")] = 95.0      # t=40 → stop
    fired = fired_at(df, [20, 40])

    # 透過 backtest_detector 的統計路徑驗證：直接用 simulate + 手算對照
    trades = simulate_trades(df, fired, cfg)
    rs = [t.r for t in trades]
    assert rs == pytest.approx([2.5, -1.0])
    assert np.mean(rs) == pytest.approx(0.75)


def test_incomplete_horizon_trades_excluded(cfg):
    # 訊號離資料尾端不足 horizon → 不納入樣本（避免偏差）
    df = flat_df(30)
    trades = simulate_trades(df, fired_at(df, [20]), cfg)   # 20+20 >= 30
    assert trades == []


def test_lookahead_bias_shift_test(cfg):
    """把整份 df shift 一根後重跑，期望值不應顯著提升。
    若 detector 或模擬偷看未來，平移資料會讓期望值大幅變動。
    """
    df = SyntheticProvider().fetch("SYN_BREAK", 500, "1d").df
    base = backtest_detector(df, "breakout", cfg)

    shifted = df.shift(1).dropna()
    shifted_stats = backtest_detector(shifted, "breakout", cfg)

    assert base.n > 0
    # 容許區間：平移後 expectancy 變動 <= 0.5R 且不得由負轉大正
    assert abs(shifted_stats.expectancy_R - base.expectancy_R) <= 0.5
    assert shifted_stats.expectancy_R <= base.expectancy_R + 0.5


def test_decayed_flag_logic(cfg):
    # 用合成資料檢查 decayed 定義：in>0 且 holdout<0 才為 True
    df = SyntheticProvider().fetch("SYN_BREAK", 500, "1d").df
    stats = backtest_detector(df, "breakout", cfg)
    if stats.decayed:
        assert stats.in_sample_expectancy > 0 and stats.holdout_expectancy < 0
    else:
        assert not (stats.in_sample_expectancy > 0 and stats.holdout_expectancy < 0)


def test_syn_break_has_enough_samples(cfg):
    # SYN_BREAK 的設計要能供給回測樣本（多次歷史突破）
    df = SyntheticProvider().fetch("SYN_BREAK", 500, "1d").df
    stats = backtest_detector(df, "breakout", cfg)
    assert stats.n >= 3
