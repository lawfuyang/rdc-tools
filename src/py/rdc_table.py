"""The three shapes `--format` asks for: the terminal's own table, CSV, and a Markdown table.

Only the commands that print *rows* take a format, and `table` is not a renderer here: it is the
command's own printing -- the columns it has always printed, at the widths it chose -- which is what a
run without `--format` gets, byte for byte. `csv` and `markdown` are built from the same rows the
command hands over, as a header plus one tuple per row, so what a reader sees in a terminal and what a
spreadsheet receives cannot drift apart.

**A format changes the shape of the answer, never its selection.** The rows are the ones the command
would have printed, in the same order, with the same caps (`summary` still lists the top 40 chunk types
and the first 120 markers, `resources` still honours its limit) -- only their arrangement changes, so a
number read out of a CSV is a number the terminal would have shown too.

Two conventions worth stating out loud:

* in `csv` and `markdown`, **stdout is the table alone**, and the lines the table form prints around it
  (`resources: 542 ids (516 with a descriptor, 528 named)`, `total resources: 542 (shown 40)`) go to
  *stderr*: a prose line in the middle of a CSV is not a row, and that is what makes the output
  pasteable into a spreadsheet or an issue;
* the CSV is RFC 4180 as `csv.writer` writes it -- a value holding a comma, a quote or a newline is
  quoted and an inner quote is doubled -- with `\\n` line endings, so a diff or a git checkout does not
  rewrite every line.
"""

from __future__ import annotations

import csv
import sys

from typing import Any, Iterable, List, Sequence, Tuple

#: The `--format` values, in the order the usage lines list them.
FORMATS: Tuple[str, ...] = ('table', 'csv', 'markdown')

def valid(name: str) -> bool:
    """Whether `name` is a format at all; the entry point prints a usage line when it is not."""
    return name in FORMATS

def emit(fmt: str, header: Sequence[str], rows: Iterable[Sequence[Any]],
         notes: Sequence[str] = ()) -> None:
    """Print `rows` as `csv` or `markdown`, with `notes` on stderr (see the module docstring).

    `table` is deliberately not accepted: it is the command's own printing, and every caller reaches
    this with the non-`table` value it already checked (`valid`).
    """
    for note in notes:
        print(note, file=sys.stderr)
    table: List[List[str]] = [[_cell(value) for value in row] for row in rows]
    if fmt == 'csv':
        writer = csv.writer(sys.stdout, lineterminator='\n')
        writer.writerow([str(name) for name in header])
        writer.writerows(table)
        return
    if fmt != 'markdown':
        raise ValueError('not a row format: %r' % (fmt,))
    print('| ' + ' | '.join(str(name) for name in header) + ' |')
    print('|' + '|'.join('---' for _ in header) + '|')
    for row in table:
        print('| ' + ' | '.join(_markdown_cell(value) for value in row) + ' |')

def _cell(value: Any) -> str:
    """One value as text: `None` is an empty cell rather than the word "None"."""
    return '' if value is None else str(value)

def _markdown_cell(value: str) -> str:
    """A cell for a Markdown table: a `|` would end the cell and a newline would end the row."""
    return value.replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')

__all__ = [
    'FORMATS',
    'emit',
    'valid',
]
