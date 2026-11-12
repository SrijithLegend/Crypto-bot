import warnings

import numpy as np
import pytest

import scanner as S
from funding import funding_vote
from risk import Risk
from test_bot import signal_df

BASE = np.random.default_rng(0).normal(0.01, 0.004, 1000)


def test_funding_vote_contrarian_and_fail_open():
    assert funding_vote(np.r_[BASE, 0.05])[0] == -1        # longs crowded -> vote short
    assert funding_vote(np.r_[BASE, -0.05])[0] == 1        # shorts crowded -> vote long
    assert funding_vote(np.r_[BASE, 0.01])[0] == 0
    assert funding_vote([0.01] * 2000)[0] == 0 and funding_vote(BASE[:50])[0] == 0 and funding_vote([])[0] == 0


def test_entry_modes():
    df, s = signal_df()
    res = S.scan(df, S.Config())
    d = 1 if s.side == "LONG" else -1
    assert S.consensus(df, res, 0, 3, "scanner")["side"] == s.side
    assert S.consensus(df, res, -d, 3, "scanner") is not None       # scanner mode ignores funding
    assert S.consensus(df, res, 0, 3, "gated") is not None          # funding abstains -> allowed
    assert S.consensus(df, res, d, 3, "gated") is not None          # funding agrees -> allowed
    assert S.consensus(df, res, -d, 3, "gated") is None             # funding opposes -> veto
    assert S.consensus(df, res, 0, 5, "vote") is None               # 5 of 5 not met
    v = S.consensus(df, res, 0, 3, "vote")
    assert v is None or "signal" in v["votes"]                      # stage 4 must be among the voters


def test_vote_edge_trigger():
    df, s = signal_df()
    res = S.scan(df, S.Config())
    n = len(df) - 1
    # same vote set on previous bar -> no re-entry
    orig = S.stage_votes
    S.stage_votes = lambda df_, r, i, f=0: {"trend": 1, "bos": 1, "ma": 1, "signal": 1, "funding": 0} if s.side == "LONG" \
        else {"trend": -1, "bos": -1, "ma": -1, "signal": -1, "funding": 0}
    try:
        assert S.consensus(df, res, 0, 3, "vote") is None
    finally:
        S.stage_votes = orig


def test_risk_validation():
    Risk(entry_mode="gated").validate()
    for bad in (dict(entry_mode="x"), dict(min_votes=2)):
        with pytest.raises(ValueError):
            Risk(**bad).validate()
    with pytest.warns(UserWarning):
        Risk(leverage=20, margin_use_pct=100, target_equity_pct=20).validate()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        Risk().validate()                                           # safe defaults: no warning
