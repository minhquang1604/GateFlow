"""One module per phase of the closed loop.

Each step takes a :class:`~demo.context.DemoContext`, performs its part
of the lifecycle through the framework's own APIs, records what it did,
and returns. Steps are independently importable and testable — the
runner sequences them, it does not contain them.
"""
