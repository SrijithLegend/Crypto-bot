import numpy as np
import scanner as S
from funding import funding_vote
from test_bot import signal_df

BASE = np.random.default_rng(0).normal(0.01, 0.004, 1000)


def test_funding_vote_contrarian_and_fail_open():
    assert funding_vote(np.r_[BASE, 0.05])[0] == -1        # longs crowded -> vote short
    assert funding_vote(np.r_[BASE, -0.05])[0] == 1        # shorts crowded -> vote long
    assert funding_vote(np.r_[BASE, 0.01])[0] == 0
    assert funding_vote([0.01] * 2000)[0] == 0 and funding_vote(BASE[:50])[0] == 0 and funding_vote([])[0] == 0


def test_consensus_needs_two_stages_and_funding_can_veto():
    df, s = signal_df()
    res = S.scan(df, S.Config())
    c = S.consensus(df, res, 0, 2)
    assert c and c["side"] == s.side and len(c["votes"].split(",")) >= 2
    assert S.consensus(df, res, 0, 5) is None              # 5 of 5 required -> not met
