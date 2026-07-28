"""FEDERaiDE - AI Agent with TUI interface."""

import importlib.metadata
try:
    __version__ = importlib.metadata.version("federaide")
except Exception:
    __version__ = "unknown"
    
__app_name__ = "federaide"