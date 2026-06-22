"""Paper-trading ("모의투자") engine — a simulated daily book per market that
trades the logged LIVE predictions, logs NAV/returns over time, and feeds a
weekly advisory algorithm-improvement report. Isolated from the real Toss
portfolio (separate ``data/paper_trading.db``)."""
