"""
Bearish Hedge Executor.
Strategy: SHORT futures + nearest ITM CALL as hedge.

At window open: enters the verify loop and sells futures only when the ITM CALL,
premium, time value, spread, stale-feed guard, and strike-clash checks pass.
"""

from backend.agents.base_executor import BaseExecutor


class BearishExecutor(BaseExecutor):
    def __init__(self, is_paper: bool = False, force_window: bool = False,
                 paper_engine=None):
        super().__init__(
            name="BearishExecutor" + ("_Paper" if is_paper else ""),
            direction="BEARISH",
            is_paper=is_paper,
            force_window=force_window,
            paper_engine=paper_engine,
        )

    @property
    def _cfg_prefix(self) -> str:
        return "bear_"

    @property
    def _futures_side(self) -> str:
        return "SELL"

    @property
    def _option_side(self) -> str:
        return "C"

    def _is_eligible(self, price: float, _target_line: float) -> bool:
        return True
