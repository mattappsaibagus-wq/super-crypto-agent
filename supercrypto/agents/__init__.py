"""Agent implementations. Each agent emits typed signals onto the shared bus."""

from supercrypto.agents.sentiment import SentimentAgent
from supercrypto.agents.pattern import PatternAgent
from supercrypto.agents.correlation import CorrelationAgent
from supercrypto.agents.meta_learner import MetaLearner

__all__ = [
    "SentimentAgent",
    "PatternAgent",
    "CorrelationAgent",
    "MetaLearner",
]
