"""Installed as the `benchpanel` package: `from benchpanel import Bench`.

The code is client/benchpanel.py, which also works on its own when copied next to
a script or found through PYTHONPATH=.../client -- both routes import the same file.
"""
from .benchpanel import Bench, BenchError, __version__  # noqa: F401
