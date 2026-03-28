"""guardian-tap: One-line integration to make any FastAPI WebSocket app observable.

Usage:
    from guardian_tap import attach_observer
    attach_observer(app)
"""

from .core import attach_observer

__all__ = ["attach_observer"]
__version__ = "0.1.0"
