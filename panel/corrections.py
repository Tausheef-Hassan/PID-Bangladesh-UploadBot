# corrections.py
# The name-corrections table (translation_replacements.tsv) as rows the panel
# can list, add, edit and delete. Comment lines are preserved; rows are keyed
# by their Bengali, which the bot matches on, so two rows can't share one.

import os
from datetime import date
from pathlib import Path

from src.translator import SEPARATOR, parse_replacement_line


def _check(*fields):
    for f in fields:
        if not f or not f.strip() or SEPARATOR in f or '\n' in f or '\r' in f:
            raise ValueError(f'Not a usable value: {f!r}')


def _lines(path):
    try:
        return Path(path).read_text(encoding='utf-8').splitlines()
    except OSError:
        return []


def _write(path, lines):
    # Write aside, then swap in: the bot reads this file over NFS, and a table
    # caught half-written would mean a run with names missing.
    tmp = f'{path}.tmp'
    Path(tmp).write_text('\n'.join(lines) + '\n', encoding='utf-8')
    os.replace(tmp, path)


def _line(bn, en, user):
    return SEPARATOR.join([bn.strip(), en.strip(), user or '', date.today().isoformat()])


def rows(path):
    out = []
    for line in _lines(path):
        parsed = parse_replacement_line(line)
        if parsed:
            out.append(dict(zip(('bn', 'en', 'user', 'date'), parsed)))
    return out


def add(path, bn, en, user):
    """Append a row. False, and nothing written, if that Bengali is already there."""
    _check(bn, en)
    if any(r['bn'] == bn.strip() for r in rows(path)):
        return False
    _write(path, _lines(path) + [_line(bn, en, user)])
    return True


def update(path, old_bn, bn, en, user):
    _check(bn, en)
    if bn.strip() != old_bn and any(r['bn'] == bn.strip() for r in rows(path)):
        return False
    lines, found = _lines(path), False
    for i, line in enumerate(lines):
        parsed = parse_replacement_line(line)
        if parsed and parsed[0] == old_bn:
            lines[i], found = _line(bn, en, user), True
    if found:
        _write(path, lines)
    return found


def delete(path, bn):
    lines = _lines(path)
    kept = [l for l in lines if (parse_replacement_line(l) or ('',))[0] != bn]
    if len(kept) == len(lines):
        return False
    _write(path, kept)
    return True
