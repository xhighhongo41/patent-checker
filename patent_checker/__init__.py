"""Patent prior-art exploration toolkit: core library and CLI."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("patent-checker")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
