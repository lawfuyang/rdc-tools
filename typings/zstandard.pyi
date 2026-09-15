"""Minimal stub for the optional `zstandard` dependency.

Only the surface rdc_analysis.py actually uses is declared, so the module stays an optional
runtime dependency while Pyright/Pylance can still resolve it in Standard mode.
"""

from typing import BinaryIO

class ZstdDecompressor:
    def __init__(self) -> None: ...
    def stream_reader(self, source: bytes) -> BinaryIO: ...
