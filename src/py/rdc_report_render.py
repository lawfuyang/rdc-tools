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
        '(REFERENCE §9).',
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
        'state, one over the resource table and three over the capture\'s chunk stream -- and a finding is '
        'proven only where the corpus carries a cause for it (the capture\'s `known` list, matched by the '
        'capture\'s own SHA-256, REFERENCE §4.17); everything else is unproven, a lead rather than a verdict. '
        'What is not checked at all is stated rather than approximated: MSAA\'s *which '
        'subresource did the resolve copy* half needs the ResolveSubresource payload, and the sRGB/linear half '
        'of the format rule needs a later sampling view\'s sRGB flag -- neither is in a bundle. The pipeline '
        'state is recorded '
        'per state *change*, not per event (the frame table above gives this bundle\'s count of state documents '
        'against its count of events), so a state rule is stated per range rather than per event; and a bundle '
        'written by an older driver carries no '
        'pipeline state at all, so those five rules are reported as not looked at rather than as clean. Two '
        'things the binding rules deliberately do not claim: the *resource type* a reflection row declares '
        '(texture against buffer -- the row names a binding, not its type), and a range/heap disagreement at '
        'a register no shader reads. A bundle whose driver did not resolve descriptor tables carries no slot '
        'rows, and the rules that read them are then reported as not looked at rather than as clean.',
        'The notable lists rank what a bundle can measure -- calls, target bytes, resource churn, and counter '
        'cost when the bundle was written with --with-counters -- and their own table says so where an input is '
        'not available: a draw\'s vertex count is the first input of the rule and no bundle has it, because the '
        'replay API exposes no action list (REFERENCE §9). A recommendation is a lead, not a verdict: it names '
        'the first instance of something, with the command that shows it, and the finding behind it is still '
        'unproven.',
        'Missing reflection is not reported as missing: a shader the engine has no reflection for is simply a '
        'shader with no constant blocks or bindings here, which is why the pipeline-state rules state which '
        'blocks and signatures they read rather than claiming a clean result from an empty one. Shader '
        'debugging is a capture property, not a bundle one: nothing here can say what a shader computed from '
        'its inputs, only what it was bound to.',
        'The frame was replayed on this machine\'s GPU: the capture properties in the bundle say whether the '
        'replay was local and which vendor it was, and device-specific behaviour is out of reach (ROADMAP §3, '
        'remote replay). A pass is also not a *dispatch* of work in the engine\'s own terms -- the report groups '
        'events, and the engine\'s own pass structure is only as close as its markers are.',
        'The usage chain is the engine\'s record, not the frame\'s intention: one row is one usage (a buffer '
        'bound to eight slots has eight rows at one eid), and the list stops at the capture -- a read by the '
        'next frame or by the CPU afterwards looks exactly like nothing ever reading the resource. A resource '
        'whose only row is `eid 0, Unused` was not tracked by the engine and is never judged: what the engine '
        'did not record cannot be turned into either a use or an absence of one.',
        'Counters are not folded into the pass sections (REFERENCE §9). A bundle written with --with-counters '
        'carries the per-event results in counters.json, and the notable ranking sums them per pass, but no pass '
        'roll-up prints them.',
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

def _input_block(inputs: List[NotableInput], rules: List[str], limit: int, oddity_limit: int) -> List[str]:
    """The rule a notable list was built by: the ranking key, then the rules that ignore the ranking.

    Printed before the rows for the same reason the caveats are printed at all -- a ranking whose inputs are
    invisible cannot be disagreed with, and this one is a choice. An input the bundle could not answer stays in
    the table with the reason, because a ranking that quietly did without it is a claim about the frame made
    from data that was never there.
    """
    lines: List[str] = []
    lines.append('Ranked by, most significant first -- this is the order of the rows below:')
    lines.append('')
    lines.append('| input | measured as | in this bundle |')
    lines.append('|---|---|---|')
    for entry in inputs:
        state = 'read' if entry['available'] else '**not available** -- %s' % _md(entry['why'])
        lines.append('| %s | %s | %s |' % (_md(entry['input']), _md(entry['how']), state))
    lines.append('')
    lines.append('Listed whatever their rank:')
    lines.append('')
    for rule in rules:
        lines.append('- %s' % _md(rule))
    lines.append('')
    lines.append('The ranking lists at most %d and the rules at most %d more; the notes say what that left '
                 'out.' % (limit, oddity_limit))
    lines.append('')
    return lines

def _notable_passes_section(notables_doc: Notables) -> List[str]:
    lines = _input_block(notables_doc['passInputs'], notables_doc['oddities'], notables_doc['limit'],
                         notables_doc['oddityLimit'])
    if not notables_doc['passes']:
        lines.append('No pass is notable: the frame has no pass for the ranking to rank and none matched a '
                     'rule above.')
        lines.append('')
        return lines
    lines.append('| # | pass | eids | why it is here | measured |')
    lines.append('|---|---|---|---|---|')
    for row in notables_doc['passes']:
        lines.append('| %s | %d | %d–%d | %s | %s |'
                     % (row['rank'] or '—', row['passIndex'], row['firstEid'], row['lastEid'],
                        '; '.join(_md(reason) for reason in row['why']),
                        '; '.join(_md(value) for value in row['values'])))
    lines.append('')
    for note in notables_doc['notes']:
        lines.append('- %s' % _md(note))
    if notables_doc['notes']:
        lines.append('')
    return lines

def _notable_resources_section(notables_doc: Notables) -> List[str]:
    lines = _input_block(notables_doc['resourceInputs'], notables_doc['resourceSpecials'],
                         notables_doc['limit'], notables_doc['oddityLimit'])
    if not notables_doc['resources']:
        lines.append('No resource is notable: nothing in the resource table is ranked, and none matched a rule '
                     'above.')
        lines.append('')
        return lines
    lines.append('| # | resource | what it is | why it is here | measured |')
    lines.append('|---|---|---|---|---|')
    for row in notables_doc['resources']:
        named = '`%s`%s' % (row['resource'], ' "%s"' % _md(row['name']) if row['name'] else '')
        lines.append('| %s | %s | %s | %s | %s |'
                     % (row['rank'] or '—', named, '%s, %s' % (row['kind'], _md(row['detail'])),
                        '; '.join(_md(reason) for reason in row['why']),
                        '; '.join(_md(value) for value in row['values'])))
    lines.append('')
    return lines

def _severity_block(table: List[SeverityRow]) -> List[str]:
    """The rule the findings are grouped by: which group means what, and who put each detector in its group.

    Severity is the tool's opinion, not the engine's, so it is printed with the line that justifies it line by
    line -- a reader who thinks a group is wrong can name the detector they disagree about.
    """
    lines: List[str] = []
    if not table:
        return lines
    lines.append('Grouped by severity: how much a finding of that kind would matter *if it is real*. That is '
                 'this tool\'s judgement rather than the engine\'s, and it is declared per detector here so a '
                 'reader can disagree with a line of it rather than with an ordering:')
    lines.append('')
    lines.append('| group | what it means | detectors, and why each is in the group |')
    lines.append('|---|---|---|')
    for row in table:
        members = ' · '.join('`%s` — %s' % (_md(item['detector']), _md(item['why']))
                             for item in row['members'])
        lines.append('| **%s** | %s | %s |' % (_md(row['severity']), _md(row['means']), members))
    lines.append('')
    return lines

def _recommendations_section(todo: Recommendations) -> List[str]:
    """What to look at first, ranked, each with the command that shows its evidence."""
    lines: List[str] = []
    if not todo['rows']:
        lines.append('Nothing to recommend: no detector fired, no oddity rule matched and no gap was left '
                     'open. That is a statement about these checks, not about the frame -- the caveats below '
                     'say what was not checked at all.')
        lines.append('')
        return lines
    lines.append('Ranked by the same severity as the findings, then findings before structure notes before '
                 'gaps. Each row is one thing to look at -- not one finding: a capture with twenty-one dead '
                 'allocations has one row here, and the findings section has them all.')
    lines.append('')
    lines.append('| # | what to look at | why | command |')
    lines.append('|---|---|---|---|')
    for row in todo['rows']:
        lines.append('| %d | %s | %s | `%s` |' % (row['rank'], _md(row['do']), _md(row['why']),
                                                   _md(row['command'])))
    lines.append('')
    lines.append('`<dir>` in a command is where you want a bundle written. Where a finding\'s evidence names no '
                 'event or resource for its command to aim at, the command is the frame-level one -- the action '
                 'tree names every event, so it is always a way to find the one the row is about.')
    lines.append('')
    for note in todo['notes']:
        lines.append('- %s' % _md(note))
    if todo['notes']:
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
                 'message(s). Of those, %d pass(es) and %d resource(s) are notable, %d finding(s) fired and '
                 '%d recommendation(s) came out of them (REFERENCE §4.11).'
                 % (manifest.get('fileCount'), manifest.get('bundleVersion'),
                    _md(str(manifest.get('driver'))), _md(str(manifest.get('renderdoc'))),
                    frame['events'], len(doc['passes']), frame['resources'], frame['messages'],
                    len(doc['notables']['passes']), len(doc['notables']['resources']),
                    len(doc['flags']), len(doc['recommendations']['rows'])))
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
    # What was and was not analysed (REFERENCE §4.11): the counts to check the detector runs and the notable
    # lists against, and the capture properties that decide what *can* be checked at all.
    lines.append('| capture properties | api `%s` (pipelineType %d), local replay %s, vendor id %d, shader '
                 'debugging %s, pixel history %s, counters %s |'
                 % (_md(str(frame['api'])), frame['pipelineType'], 'yes' if frame['localRenderer'] else 'no',
                    frame['vendor'], 'available' if frame['shaderDebugging'] else 'not in the capture',
                    'available' if frame['pixelHistory'] else 'not in the capture',
                    'collected' if manifest.get('withCounters') else 'not collected (no --with-counters)'))
    ran = [run for run in doc['detectors'] if run['ran']]
    lines.append('| analysed | %d of %d detector(s) ran; %s; the notable tables name every input their '
                 'ranking could not read |'
                 % (len(ran), len(doc['detectors']),
                    '%d skipped (Red flags names each with its reason)' % (len(doc['detectors']) - len(ran))
                    if len(ran) < len(doc['detectors']) else 'none skipped'))
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
    # A texture's byte count is not in the resource table -- the driver records a texture's dimensions and
    # format, not its bytes (see the notable list, which ranks them in pixels) -- so the row says what the
    # table has. Printing "0.00 MB of textures" next to a hundred of them reads as "no texture memory".
    lines.append('| bytes in resources | buffers %.2f MB; textures: no byte count in the table (it carries '
                 'their dimensions, and the notable list ranks them in pixels) |'
                 % (frame['bufferBytes'] / 1048576.0))
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
    lines.append('## Notable passes')
    lines.append('')
    lines.extend(_notable_passes_section(doc['notables']))
    lines.append('## Notable resources')
    lines.append('')
    lines.extend(_notable_resources_section(doc['notables']))
    lines.append('## Red flags')
    lines.append('')
    ran = [run['detector'] for run in doc['detectors'] if run['ran']]
    skipped = ['%s (%s)' % (run['detector'], run['why']) for run in doc['detectors'] if not run['ran']]
    lines.append('%d finding(s). Detectors that ran: %s.%s'
                 % (len(doc['flags']), ', '.join(ran) or 'none',
                    ' Skipped: %s.' % ', '.join(skipped) if skipped else ''))
    lines.append('')
    proven = sum(1 for flag in doc['flags'] if not flag['unproven'])
    lines.append('`certain` means the bundle proves the observation; `question` would mean the observation is '
                 'real but its meaning depends on what the frame was for. A finding is **proven** only where '
                 'the corpus knows its cause -- followed to the frame (or to the engine\'s answer) and written '
                 'down in the capture\'s `known` list (REFERENCE §4.17), which is where the printed cause comes '
                 'from; %d of %d finding(s) here are. Every other one is **unproven**: a lead, not a verdict.'
                 % (proven, len(doc['flags'])))
    lines.append('')
    lines.extend(_severity_block(doc['severityTable']))
    # The group of a finding is a join on its detector -- the severity table above is the only place that
    # judgement lives, so a row here cannot disagree with the table without the reader seeing both.
    group_of = {item['detector']: row['severity'] for row in doc['severityTable'] for item in row['members']}
    if doc['flags']:
        for row in doc['severityTable']:
            group = [flag for flag in doc['flags']
                     if group_of.get(flag['detector'], 'medium') == row['severity']]
            if not group:
                continue
            lines.append('### %s — %d finding(s)' % (_md(row['severity']), len(group)))
            lines.append('')
            lines.append('| detector | finding | evidence | certainty |')
            lines.append('|---|---|---|---|')
            for flag in group:
                lines.append('| %s | %s | %s | %s%s |'
                             % (flag['detector'], _md(flag['what']),
                                '; '.join(_md(item) for item in flag['evidence']), flag['certainty'],
                                ', unproven' if flag['unproven']
                                else ', **proven** (%s): %s' % (_md(str(flag.get('verdict', ''))),
                                                                _md(str(flag.get('cause', ''))))))
            lines.append('')
    else:
        lines.append('Nothing fired -- which is a statement about these detectors, not about the frame: the '
                     'caveats below say what was not checked at all.')
        lines.append('')
    lines.append('## Recommendations')
    lines.append('')
    lines.extend(_recommendations_section(doc['recommendations']))
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
    lines.append('A pass in the notable list is checked the same way, at its first event; a notable resource is '
                 'checked with `replay_dump usage \'%s\' <resId>`, which is what the read counts in that list '
                 'were computed from.' % _md(rdc))
    lines.append('')
    lines.append('Every row in this report carries the event id or the resource id it is about -- a pass by its '
                 'eid range, a resource by its `res` id, a finding by the evidence on its own row -- and the '
                 'recommendations carry the command that shows each one. A row that could not cite either would '
                 'be a claim without evidence, and the suite fails on one (AGENTS.md).')
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
