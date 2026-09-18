"""Minimal stub for the optional `zstandard` dependency.

Only the surface rdc_analysis.py actually uses is declared, so the module stays an optional
runtime dependency while Pyright/Pylance can still resolve it in Standard mode.
"""

from typing import BinaryIO, Union

#: `stream_reader` takes any buffer, not only `bytes`: a mapped or bytearray section body is handed to it
#: directly rather than being copied so the stub's narrower signature is satisfied (REFERENCE 4.14).
Source = Union[bytes, bytearray, memoryview, 'mmap.mmap']

class ZstdDecompressor:
    def __init__(self) -> None: ...
    def stream_reader(self, source: Source) -> BinaryIO: ...
