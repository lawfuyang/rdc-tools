"""How the report is written: the caveats that state its own gaps, and the Markdown -- deterministic, one table per question."""

from __future__ import annotations

from rdc_bundle import *  # noqa: F401,F403

from typing import List

def report_caveats() -> List[str]:
    """What this report cannot say, in its own words. Fixed text, in a fixed order: it is part of the
    output a reader compares between runs, and a report that quietly stops mentioning a gap is worse
    than one that never mentioned it."""
    return [
        'A pass here is a run of consecutive events with the same call kind, render targets and depth '
        'target -- not a named pass. Call kinds (draw/copy/clear/marker), per-event triangle and thread '
        'counts, and marker names are not in a bundle at all: the replay API exposes no action list '
        '(ROADMAP §2).',
        'A compute pass is a run of dispatches with the same pipeline and shaders, and its targets and '
        'depth are given as not applicable: a dispatch does not set the output-merge state, so what the '
        'engine reports there is leftover from an earlier call. What a dispatch *does* write (its UAVs) '
        'is not in a bundle at all.',
        'What a pass is *for* is claimed only where the engine\'s own names say so, and every such claim is '
        'marked name-based: the tables in `engine-schemas/` map a name the capture contains -- a constant '
        'block, a shader entry point, a resource name, a marker name, a pass structure string -- onto a '
        'concept, and a concept with no name behind it is never claimed. A bundle whose names match no table '
        'gets no interpretation at all rather than a guess, and the section says so.',
        'A marker name is evidence only once a bundle carries one: the driver records the engine\'s marker '
        'path per event as of 2026-09-17, so a bundle written before that claims no marker-based concept, '
        'and the section says which concepts that costs. The values printed with a concept are the members '
        'the table tags, as the bundle\'s cbuffer documents hold them: what the engine reported at that '
        'event, not what the shader made of it.',
        'Twenty detectors run -- seven over the bundle, four over the usage chain, five over the pipeline '
        'state, one over the resource table and three over the capture\'s chunk stream -- and every finding '
        'is unproven: none of them has been checked against a capture whose bug list is known (ROADMAP §1.3). '
        'What is not checked at all is stated rather than approximated: MSAA\'s *which '
        'subresource did the resolve copy* half needs the ResolveSubresource payload, and the sRGB/linear half '
        'of the format rule needs a later sampling view\'s sRGB flag -- neither is in a bundle. The pipeline '
        'state is recorded '
        'per state *change*, not per event (30 documents for the measured capture\'s 723 events), so a state '
        'rule is stated per range rather than per event; and a bundle written by an older driver carries no '
        'pipeline state at all, so those five rules are reported as not looked at rather than as clean. Two '
        'things the binding rules deliberately do not claim: the *resource type* a reflection row declares '
        '(texture against buffer -- the row names a binding, not its type), and a range/heap disagreement at '
        'a register no shader reads. A bundle whose driver did not resolve descriptor tables carries no slot '
        'rows, and the rules that read them are then reported as not looked at rather than as clean.',
        'Ranked notables and recommendations are not implemented yet either (ROADMAP §1.1, §1.2).',
        'The usage chain is the engine\'s record, not the frame\'s intention: one row is one usage (a buffer '
        'bound to eight slots has eight rows at one eid), and the list stops at the capture -- a read by the '
        'next frame or by the CPU afterwards looks exactly like nothing ever reading the resource. A resource '
        'whose only row is `eid 0, Unused` was not tracked by the engine and is never judged (101 of the 133 '
        'resources in the measured capture).',
        'Counters are not folded per pass yet (ROADMAP §4). A bundle written with --with-counters '
        'carries the per-event results in counters.json.',
        'Blend, depth-test, stencil, viewport and scissor state are in the bundle per state change, and the '
        'state given per pass is still the bound shaders and their constant blocks: a pass is a run of events '
        'and the state can change inside one, so the state rules name the eid range they were read at. The '
        'events listed as having a state document carry the full D3D12 state.',
        'Nothing here samples or decodes a texture or a render target: formats are named, pixels are '
        'not read.',
        'A bundle is a cache of what the engine said in one session. If the capture changed since it '
        'was written, the manifest still carries the hash of the capture it was written from (the '
        'provenance table above), and the report is about that capture.',
    ]

def _engine_section(engine: EngineInterpretation) -> List[str]:
    """The engine's vocabulary: what the capture's own names said, and the values the table tags for them.

    Nothing here is computed twice -- the interpretation arrives assembled (see `rdc_engine_schema`) and this
    only lays it out. Every row keeps the name it rests on in the same line as the concept, because a reader
    who disagrees with a concept needs to see what it was claimed from without leaving the report.
    """
    lines: List[str] = []
    if not engine['engine']:
        lines.append('No engine was recognised by name in this bundle, so no pass is named for one: a frame is '
                     'described in an engine\'s vocabulary only when the capture itself carries names the '
                     'table lists. The structure above and the passes below are what can be said without it.')
        lines.append('')
        for reason in engine['notInterpreted']:
            lines.append('- %s' % _md(reason))
        lines.append('')
        return lines

    lines.append('**%s**, read from `engine-schemas/%s` — %s.'
                 % (_md(engine['engine']), _md(engine['schema']), _md(engine['basis'])))
    lines.append('')
    lines.append('| recognised by | name | where |')
    lines.append('|---|---|---|')
    for item in engine['detected']:
        lines.append('| %s | `%s` | %s |' % (_md(item['kind']), _md(item['name']), _md(item['where'])))
    lines.append('')
    if engine['concepts']:
        lines.append('| concept | kind | claimed for | because |')
        lines.append('|---|---|---|---|')
        for row in engine['concepts']:
            where = ('pass %d (eid %d)' % (row['passIndex'], row['firstEid'])
                     if row['passIndex'] else 'the frame')
            because = '; '.join('%s `%s`' % (_md(item['kind']), _md(item['name']))
                                for item in row['evidence'])
            lines.append('| %s | %s | %s | %s |' % (_md(row['concept']), _md(row['kind']), where, because))
    else:
        lines.append('No concept in the table matched this frame\'s names.')
    lines.append('')
    # What a concept *is* does not depend on the pass it was claimed for, so its note is printed once however
    # many rows it produced.
    said: List[str] = []
    for row in engine['concepts']:
        if row.get('note') and row['concept'] not in said:
            said.append(row['concept'])
            lines.append('- `%s`: %s' % (_md(row['concept']), _md(str(row['note']))))
    if said:
        lines.append('')

    for question in engine['questions']:
        lines.append('### %s' % _md(question['title']))
        lines.append('')
        if question['ask']:
            lines.append(_md(question['ask']))
            lines.append('')
        if not question['conceptRows']:
            lines.append('Nothing in this frame\'s names speaks to it.')
            lines.append('')
            continue
        lines.append('The table\'s concepts for it, as this frame\'s names claim them:')
        lines.append('')
        for row in question['conceptRows']:
            where = ('pass %d (eid %d)' % (row['passIndex'], row['firstEid'])
                     if row['passIndex'] else 'the frame')
            lines.append('- %s — %s' % (_md(row['concept']), where))
        lines.append('')
        if question['values']:
            lines.append('| pass | eid | cbuffer | member | value |')
            lines.append('|---|---|---|---|---|')
            for item in question['values']:
                # A member read while nothing was bound is its default, not its value: the row says which one
                # it is, because "this block is zero" and "this block was not bound here" are different facts.
                value = _md(item['value']) if item['bound'] else '*not bound* (the read is every member\'s default)'
                lines.append('| %s | %d | `%s` | `%s` | %s |'
                             % (item['passIndex'] or '—', item['firstEid'], _md(item['block']),
                                _md(item['member']), value))
            lines.append('')
        else:
            lines.append('The table tags no member of the blocks this frame binds for that question, so the '
                         'values behind it are not read.')
            lines.append('')
    for reason in engine['notInterpreted']:
        lines.append('- not interpreted: %s' % _md(reason))
    if engine['notInterpreted']:
        lines.append('')
    return lines

def render_report_markdown(doc: ReportDocument, rdc: str) -> str:
    """The report as Markdown. Deterministic: same bundle in, same bytes out."""
    manifest = doc['bundle']
    frame = doc['frame']
    capture_name = doc['capture'] or '(unknown)'
    lines: List[str] = []

    lines.append('# Frame report — %s' % _md(capture_name))
    lines.append('')
    # No directory paths here: the prose is compared byte-for-byte between runs and machines, so it
    # names the bundle by what it holds (the manifest's own counts) rather than by where it sits. The
    # JSON twin carries `bundleDir` for a tool that needs to know.
    lines.append('Written from a bundle of %s file(s) (manifest v%s, driver %s, RenderDoc %s): %d '
                 'event(s) with bound state, %d state-derived pass(es), %d resource(s), %d debug '
                 'message(s).' % (manifest.get('fileCount'), manifest.get('bundleVersion'),
                                  _md(str(manifest.get('driver'))), _md(str(manifest.get('renderdoc'))),
                                  frame['events'], len(doc['passes']), frame['resources'],
                                  frame['messages']))
    lines.append('')
    lines.append('## Provenance')
    lines.append('')
    lines.append('| field | value |')
    lines.append('|---|---|')
    lines.append('| capture | %s |' % _md(capture_name))
    lines.append('| capture sha256 | %s |' % _md(doc['captureSha256'] or '(not in the manifest)'))
    lines.append('| renderdoc | %s |' % _md(str(manifest.get('renderdoc'))))
    lines.append('| driver | %s |' % _md(str(manifest.get('driver'))))
    lines.append('| bundle | version %s, ids %s..%s, --with-images %s, --with-counters %s, resource '
                 'usage %s |' % (manifest.get('bundleVersion'), manifest.get('since'),
                                 manifest.get('until'), manifest.get('withImages'),
                                 manifest.get('withCounters'), _md(str(manifest.get('resourceUsage')))))
    lines.append('| files read | capture.json, events.json, resources.json, messages.json, '
                 'manifest.json, %d state document pair(s) |' % frame['stateDocuments'])
    lines.append('')
    lines.append('## Frame at a glance')
    lines.append('')
    lines.append('| what | value |')
    lines.append('|---|---|')
    lines.append('| events with bound state | %d |' % frame['events'])
    lines.append('| passes (state-derived) | %d |' % len(doc['passes']))
    lines.append('| graphics / compute events | %d / %d |' % (frame['graphicsEvents'],
                                                              frame['computeEvents']))
    lines.append('| resources | %d (%s) |' % (frame['resources'], ', '.join(
        '%d %s' % (count, kind) for kind, count in sorted(frame['resourcesByKind'].items()))))
    lines.append('| bytes in resources | %.2f MB of textures, %.2f MB of buffers |'
                 % (frame['textureBytes'] / 1048576.0, frame['bufferBytes'] / 1048576.0))
    lines.append('| render targets seen | %s |' % (', '.join(frame['targetsSeen']) or 'none'))
    lines.append('| formats seen | %s |' % (', '.join(frame['formatsSeen']) or 'none'))
    lines.append('| debug messages | %d%s |' % (frame['messages'], (
        ' (' + ', '.join('%s %d' % (k, v) for k, v in frame['messagesBySeverity'].items()) + ')')
        if frame['messages'] else ''))
    lines.append('| chunks in the capture | %d |' % frame['chunks'])
    lines.append('')
    lines.append('## Pipeline map')
    lines.append('')
    lines.append('| # | eids | marker | kind | events | targets | depth | structure |')
    lines.append('|---|---|---|---|---|---|---|---|')
    for entry in doc['passes']:
        # The depth column says the same thing the pass section does for a dispatch: not applicable,
        # for the same reason the targets do.
        depth = ('n/a' if entry['kind'] == 'compute' else
                 'res%s' % _res_id(entry['depth']) if _is_resource(entry['depth']) else 'none')
        # The marker's *own* name, not the whole path: the column's job is to say which of the frame's
        # markers this pass opens under, and the nest is in the section below.
        leaf = str(entry.get('marker', '')).split(' > ')[-1] if entry.get('marker') else '—'
        lines.append('| %d | %d–%d | %s | %s | %d | %s | %s | %s |'
                     % (entry['index'], entry['firstEid'], entry['lastEid'], _md(leaf), entry['kind'],
                        entry['events'], _targets_text(entry, True), depth, entry['structure']))
    lines.append('')
    lines.append('Passes and the targets they write:')
    lines.append('')
    lines.append('```mermaid')
    lines.append('graph LR')
    for entry in doc['passes']:
        node = 'P%d["pass %d (eid %d)"]' % (entry['index'], entry['index'], entry['firstEid'])
        if entry['kind'] == 'compute' or not entry['targets']:
            lines.append('  %s' % node)
            continue
        for target in entry['targets']:
            idtext = _res_id(target.split()[0])
            lines.append('  %s --> T%s["res%s"]' % (node, idtext, idtext))
    lines.append('```')
    lines.append('')
    lines.append('## The engine\'s vocabulary')
    lines.append('')
    lines.extend(_engine_section(doc['engine']))
    lines.append('## Red flags')
    lines.append('')
    ran = [run['detector'] for run in doc['detectors'] if run['ran']]
    skipped = ['%s (%s)' % (run['detector'], run['why']) for run in doc['detectors'] if not run['ran']]
    lines.append('%d finding(s). Detectors that ran: %s.%s'
                 % (len(doc['flags']), ', '.join(ran) or 'none',
                    ' Skipped: %s.' % ', '.join(skipped) if skipped else ''))
    lines.append('')
    lines.append('`certain` means the bundle proves the observation; `question` would mean the observation is '
                 'real but its meaning depends on what the frame was for. Every finding is **unproven**: none '
                 'of these detectors has been checked against a capture whose bugs are known (ROADMAP §1.3), '
                 'so they are leads, not verdicts.')
    lines.append('')
    if doc['flags']:
        lines.append('| detector | finding | evidence | certainty |')
        lines.append('|---|---|---|---|')
        for flag in doc['flags']:
            lines.append('| %s | %s | %s | %s%s |'
                         % (flag['detector'], flag['what'], '; '.join(flag['evidence']), flag['certainty'],
                            ', unproven' if flag['unproven'] else ''))
    else:
        lines.append('Nothing fired -- which is a statement about these detectors, not about the frame: the '
                     'caveats below say what was not checked at all.')
    lines.append('')
    lines.append('## Pass by pass')
    for entry in doc['passes']:
        lines.append('')
        lines.append('### Pass %d — eid %d–%d (%s)'
                     % (entry['index'], entry['firstEid'], entry['lastEid'], entry['kind']))
        lines.append('')
        lines.append('- starts here because %s' % entry['reason'])
        if entry.get('marker'):
            lines.append('- the engine put it under `%s` (from the action list, not from state)'
                         % _md(str(entry['marker'])))
        lines.append('- work: %d event(s) (%d graphics, %d compute) — events, not vertices: the bundle '
                     'carries no counts' % (entry['events'], entry['graphics'], entry['compute']))
        lines.append('- targets: %s' % _targets_text(entry))
        if entry['kind'] != 'compute':
            lines.append('- depth: %s' % (entry['depth'] if _is_resource(entry['depth']) else 'none'))
        lines.append('- structure: %s' % entry['structure'])
        if entry['shaders']:
            lines.append('- shaders (from states/%d.state.json): %s'
                         % (entry['firstEid'], ', '.join(entry['shaders'])))
        if entry['otherShaders']:
            lines.append('- also bound at that event, and not used by a %s: %s'
                         % ('dispatch' if entry['kind'] == 'compute' else 'draw',
                            ', '.join(entry['otherShaders'])))
        if entry['blocks']:
            lines.append('- constant blocks (from states/%d.shaders.json):' % entry['firstEid'])
            for block in entry['blocks']:
                lines.append('  - %s' % block)
        if entry['firstTouched']:
            lines.append('- resources first used here (%d):' % len(entry['firstTouched']))
            for described in entry['firstTouched']:
                lines.append('  - %s' % described)
        else:
            lines.append('- no resource is used here for the first time')
    lines.append('')
    lines.append('## What this report cannot tell you')
    lines.append('')
    for caveat in doc['caveats']:
        lines.append('- %s' % caveat)
    lines.append('')
    lines.append('## Appendix — reproduce any claim')
    lines.append('')
    lines.append('| pass | commands |')
    lines.append('|---|---|')
    for entry in doc['passes']:
        first = entry['firstEid']
        lines.append("| %d | `replay_dump state '%s' %d` · `replay_dump shaders '%s' %d` |"
                     % (entry['index'], _md(rdc), first, _md(rdc), first))
    lines.append('')
    lines.append('A usage finding is checked the same way: `replay_dump usage \'%s\' <resId>` prints the same '
                 'list the detectors read (the engine\'s own `GetUsage`).' % _md(rdc))
    lines.append('')
    lines.append("Regenerate the bundle itself with `replay_dump dump '%s' <dir>`; run one replay at a "
                 'time (REFERENCE §9).' % _md(rdc))
    lines.append('')
    return '\n'.join(lines)

__all__ = [
    'render_report_markdown',
    'report_caveats',
]
