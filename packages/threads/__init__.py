"""Threads: continuous work made of independent runs.

A thread groups runs; it never replaces one. Every turn is still a full
``AgentRun`` with its own trace, cost, approvals and replay, which is the whole
reason the two are not merged.
"""
