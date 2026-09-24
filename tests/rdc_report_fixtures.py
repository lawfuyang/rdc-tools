"""The bundle fixture `test_rdc_report` used to hold: `event`, `resource`, `write_bundle`, `cbuffer`
and the `BundleCase` they are used from.

Split out of `tests/test_rdc_report.py` when it grew past 1,700 lines, because four other test
files import these builders (`test_rdc_ab`, `test_rdc_engine_schema`, `test_rdc_permutations`,
`test_rdc_sigcheck`) and a fixture copied into five places is five fixtures that drift. The
builders themselves are unchanged; only their address is."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from typing import Any, Callable, Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, 'src', 'py'))
sys.path.insert(0, HERE)

import rdc_analysis as R          # noqa: E402
from rdc_testcase import remove_tree   # noqa: E402


#: The capture's name as a bundle's manifest spells it: the default every builder here writes.
RDC = 'fixture.rdc'


def capture_text(func: Callable[..., object], *args: Any, **kwargs: Any) -> str:
    """Run `func` and return everything it printed on stdout."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        func(*args, **kwargs)
    return buf.getvalue()

def event(eid: int, kind: str = 'graphics', targets: Sequence[str] = (), depth: str = '0',
          pso: str = '100', shaders: str = 'vs=2348 ', marker: str = '',
          volume: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One `events.json` record, with the fields the report reads spelled out.

    `volume` is the work the call asked for, which a driver from 2026-09-22 on writes for a draw or a
    dispatch: it is left out by default, because a bundle without it is the case the report has to keep
    saying so about.
    """
    record: Dict[str, Any] = {'eid': eid, 'marker': marker, 'pso': pso, 'psoKind': kind, 'shaders': shaders,
                              'targets': list(targets), 'depth': depth, 'rootParameters': 1,
                              'state': 'deadbeef'}
    if volume is not None:
        record['volume'] = volume
    return record

def draw_volume(vertices: int, instances: int = 1, triangles: int = 0) -> Dict[str, Any]:
    """A draw's volume, as the driver writes it: triangles are the caller's, because the topology decides them."""
    return {'vertices': vertices, 'instances': instances, 'triangles': triangles}

def dispatch_volume(groups: Sequence[int], threads: int, threads_per_group: Sequence[int] = ()) -> Dict[str, Any]:
    """A dispatch's volume: workgroups, the threads they add up to, and the group size when it is not known."""
    return {'groups': list(groups), 'threadsPerGroup': list(threads_per_group), 'threads': threads}

def resource(resid: str, kind: str = 'texture', first: int = 0, name: str = '',
             **extra: Any) -> Dict[str, Any]:
    entry: Dict[str, Any] = {'resource': resid, 'name': name, 'kind': kind,
                             'usage': [{'eid': first, 'usage': 1}], 'usageCount': 1,
                             'firstEvent': first, 'lastEvent': first}
    entry.update(extra)
    return entry

def volume_resources() -> List[Dict[str, Any]]:
    """The two targets the work-volume tests use: a large colour target and a tiny one.

    They are here so a pass's footprint and its work can be made to *disagree* -- the big target costs pixels
    and the small one does not -- which is what makes the ranking's first input observable rather than a
    number that happens to sort the same way as the call count.
    """
    return [resource('10', first=100, name='SceneColour', width=1000, height=1000, depth=1, samples=1,
                     format='B8G8R8A8_UNORM'),
            resource('11', first=200, name='Tiny', width=10, height=10, depth=1, samples=1,
                     format='B8G8R8A8_UNORM')]

def write_json(root: str, name: str, doc: Any) -> None:
    path = os.path.join(root, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(doc, fh)

def write_bundle(root: str, events: Optional[List[Dict[str, Any]]] = None,
                 resources: Optional[List[Dict[str, Any]]] = None,
                 messages: Optional[List[Any]] = None,
                 manifest: Optional[Dict[str, Any]] = None,
                 capture: Optional[Dict[str, Any]] = None,
                 states: Optional[Dict[int, Dict[str, Any]]] = None,
                 cbuffers: Optional[Dict[str, Dict[str, Any]]] = None,
                 counters: Optional[List[Dict[str, Any]]] = None,
                 cost_counter: int = 1, cost_name: str = 'EventGPUDuration',
                 cost_unit: str = 'ms') -> None:
    """Write a bundle with the files the report requires, plus whatever the test cares about."""
    os.makedirs(root, exist_ok=True)
    base_manifest: Dict[str, Any] = {
        'bundleVersion': R.BUNDLE_VERSION, 'driver': 'replay_dump', 'renderdoc': '1.46',
        'capture': RDC, 'captureSha256': 'ab' * 32, 'since': 1, 'until': 0, 'maxEvents': 0,
        'withImages': 0, 'withCounters': 0, 'withTextures': 0, 'resourceUsage': 'collected',
        'files': [], 'fileCount': 0,
    }
    base_manifest.update(manifest or {})
    base_capture: Dict[str, Any] = {'capture': RDC, 'renderdoc': '1.46', 'driver': 'D3D12',
                                    'chunks': 10, 'pipelineType': 1, 'localRenderer': 1, 'vendor': 1}
    base_capture.update(capture or {})

    write_json(root, 'manifest.json', base_manifest)
    write_json(root, 'capture.json', base_capture)
    write_json(root, 'events.json', {'capture': RDC, 'events': events or [], 'total': len(events or [])})
    write_json(root, 'resources.json', {'capture': RDC, 'resources': resources or [],
                                        'total': len(resources or [])})
    write_json(root, 'messages.json', {'capture': RDC, 'messages': messages or [],
                                       'total': len(messages or [])})
    if counters is not None:
        # The shape the driver writes and `schema/counters` describes: an object per event, the counter's
        # enum, and the value read through that counter's own result type. `costCounter` names which of them
        # the pass table costs by -- a bundle holds a row per counter and summing all of them would add a
        # byte count to a duration.
        write_json(root, 'counters.json', {'capture': RDC, 'counters': counters,
                                          'total': len(counters), 'costCounter': cost_counter,
                                          'costCounterName': cost_name, 'unit': cost_unit})
    for eid, documents in (states or {}).items():
        if 'state' in documents:
            write_json(root, os.path.join('states', '%d.state.json' % eid), documents['state'])
        if 'shaders' in documents:
            write_json(root, os.path.join('states', '%d.shaders.json' % eid), documents['shaders'])
    for name, document in (cbuffers or {}).items():
        write_json(root, os.path.join('cbuffers', name), document)

def cbuffer(eid: int, stage: str = 'ps', slot: int = 0, buffer: str = '300',
            variables: Optional[List[str]] = None) -> Dict[str, Any]:
    """One `cbuffers/<eid>_<stage>_<slot>.json`, with the header the driver writes."""
    return {'capture': RDC, 'renderdoc': '1.46', 'driver': 'D3D12', 'localReplay': 1,
            'machine': 'test', 'eid': eid, 'stage': stage, 'slot': slot, 'shader': '2348',
            'buffer': buffer, 'variables': variables or []}

class BundleCase(unittest.TestCase):
    """A scratch directory per test; bundles and reports are written inside it."""

    tmp: str

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix='rdc_report_')
        self.addCleanup(self._remove_tmp)

    def _remove_tmp(self) -> None:
        remove_tree(self.tmp)

    def path(self, *parts: str) -> str:
        return os.path.join(self.tmp, *parts)

    def report(self, bundle: str, out: Optional[str] = None) -> str:
        """Run `report` and return its stdout."""
        return capture_text(R.cmd_report, RDC, bundle, out)

    def markdown(self, bundle: str, out: Optional[str] = None) -> str:
        with open(os.path.join(out or bundle, 'report.md'), encoding='utf-8') as fh:
            return fh.read()

    def document(self, bundle: str, out: Optional[str] = None) -> Dict[str, Any]:
        with open(os.path.join(out or bundle, 'report.json'), encoding='utf-8') as fh:
            data: Dict[str, Any] = json.load(fh)
        return data

    def flags(self, bundle: str, detector: str) -> List[Dict[str, Any]]:
        """One detector's findings out of the report document -- every class of detector test needs this."""
        self.passes(bundle)
        return [flag for flag in self.document(bundle)['flags'] if flag['detector'] == detector]

    def passes(self, bundle: str) -> List[R.ReportPass]:
        code = R.cmd_report(RDC, bundle, None)
        self.assertEqual(code, 0, 'report failed: %s' % self.markdown(bundle))
        return self.document(bundle)['passes']

    def reasons(self, bundle: str) -> List[str]:
        return [entry['reason'] for entry in self.passes(bundle)]

# =========================================================================== pass reconstruction

def pipeline_state(depth_enable: bool = True, depth_writes: bool = True, depth_function: str = 'LessEqual',
                   stencil_enable: bool = False, stencil_read_only: bool = False, pass_op: str = 'Keep',
                   write_mask: int = 255, blends: Optional[List[Dict[str, Any]]] = None,
                   viewports: Optional[List[Dict[str, Any]]] = None,
                   scissors: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The `outputMerger`/`viewports`/`scissors` blocks as the driver writes them, in one call so a test's
    fixture is about the rule rather than about the JSON. `pass_op` is the pass operation of both faces: the
    op that decides whether a state *writes* stencil, which is what the writer rule turns on."""
    face = {'fail': 'Keep', 'depthFail': 'Keep', 'pass': pass_op, 'function': 'AlwaysTrue',
            'compareMask': 255, 'writeMask': write_mask}
    return {
        'outputMerger': {
            'depthEnable': depth_enable, 'depthWrites': depth_writes, 'depthFunction': depth_function,
            'stencilEnable': stencil_enable, 'stencilReadOnly': stencil_read_only,
            'alphaToCoverage': False, 'independentBlend': False,
            'frontFace': dict(face), 'backFace': dict(face),
            'blends': blends if blends is not None else [],
        },
        'viewports': viewports if viewports is not None else [],
        'scissors': scissors if scissors is not None else [],
    }

def graphics_state(eid: int, depth_target: str = '2270', render_targets: Optional[List[str]] = None,
                   **rest: Any) -> Dict[str, Any]:
    """One draw's state document: the fields every rule reads, then whatever the test cares about."""
    document: Dict[str, Any] = {'eid': eid, 'depthTarget': depth_target,
                                'renderTargets': render_targets or [], 'shaders': [],
                                'rootParameters': []}
    blocks = dict(rest.pop('blocks', {}))
    document.update(pipeline_state(**blocks))
    document.update(rest)
    return document
