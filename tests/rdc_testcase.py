"""The shared fixtures and case classes for the offline tool's test suite.

Split out of `test_rdc_analysis.py`, which held the parsers and decoders for the whole tool and passed 2200
lines. Every test file imports these star-style, because they are what the tests are written against: the
capture builders, the stdout capture helpers, and the cases that point the cache and the chunk-name lookup at
a scratch directory.

Run the suite through the tool (`src/py/rdc_analysis.py selftest`), per file
(`python tests/test_rdc_analysis.py`), or with `python -m unittest discover -s tests -t tests`.
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from typing import Any, Callable, ClassVar, Dict, Optional, Sequence
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, ROOT, os.path.join(ROOT, 'src', 'py')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import rdc_chunkmap as chunkmap     #   copies it, so patching the consumer would patch the copy
import rdc_fixtures as F            # noqa: E402

def capture_text(func: Callable[..., object], *args: Any, **kwargs: Any) -> str:
    """Run `func` and return everything it printed on stdout."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        func(*args, **kwargs)
    return buf.getvalue()

def capture_all(func: Callable[..., object], *args: Any, **kwargs: Any) -> str:
    """Run `func` and return everything it printed on stdout *and* stderr.

    unittest's TextTestRunner writes its report to stderr, so the selftest command needs this.
    """
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        func(*args, **kwargs)
    return out.getvalue() + err.getvalue()

class TempDirCase(unittest.TestCase):
    #: scratch directory created in `setUp` and removed by a cleanup hook.
    tmp: str
    #: cache directory for this test, so nothing touches the real user cache (see `cache_dir`).
    cache: str

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix='rdc_unit_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cache = os.path.join(self.tmp, 'cache')
        env = mock.patch.dict(os.environ, {'RDC_CACHE_DIR': self.cache})
        env.start()
        self.addCleanup(env.stop)

    def path(self, name: str, data: Optional[bytes] = None) -> str:
        p = os.path.join(self.tmp, name)
        if data is not None:
            F.write_bytes(p, data)
        return p

    def capture_path(self, chunks: Sequence[bytes], name: str = 'capture.rdc',
                     **kw: Any) -> str:
        return self.path(name, F.capture(chunks, **kw))

class CmdCase(TempDirCase):
    """Base class for tests that need chunk *names*: points the tool at a fake `renderdoc-src`.

    Shared by both test modules (the command tests import it from here), so the fake enum tree is
    built once per test class and chunk ids are always looked up by name.
    """

    #: populated in `setUpClass` for every test in the class.
    _src_dir: ClassVar[str]
    src_root: ClassVar[str]
    names: ClassVar[Dict[int, str]]
    ids: ClassVar[Dict[str, int]]

    @classmethod
    def setUpClass(cls) -> None:
        cls._src_dir = tempfile.mkdtemp(prefix='rdc_src_')
        cls.src_root, cls.names = F.make_fake_src(cls._src_dir)
        cls.ids = F.invert(cls.names)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls._src_dir, ignore_errors=True)

    def setUp(self) -> None:
        super().setUp()
        names = mock.patch.object(chunkmap, 'load_chunk_names',
                                  lambda src_root=None, driver='D3D12': dict(self.names))
        src = mock.patch.object(chunkmap, 'RENDERDOC_SRC', self.src_root)
        names.start()
        src.start()
        self.addCleanup(names.stop)
        self.addCleanup(src.stop)

    def ch(self, name: str, payload: bytes = b'', **kw: Any) -> bytes:
        return F.chunk(self.ids[name], payload, **kw)

    def cap(self, *chunks: bytes, **kw: Any) -> str:
        return self.path('capture.rdc', F.capture(chunks, **kw))
