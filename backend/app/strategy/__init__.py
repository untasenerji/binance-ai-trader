"""Research-only strategies and candidate gating; this package has no order interface."""

from app.strategy.gate import CandidateGate, CandidateGateContext
from app.strategy.models import Candle, SignalCandidate

__all__ = ["CandidateGate", "CandidateGateContext", "Candle", "SignalCandidate"]
