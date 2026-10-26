"""Reviewed endpoint migrations. A shared slug alone is not employer identity."""
import json
import re
from pathlib import Path

BOARD = re.compile(r'(Ashby|Greenhouse|Lever): ([A-Za-z0-9_-]{1,150})$')

def load_audit(path=None):
    data = json.loads(Path(path or Path(__file__).with_name('board-audit.json')).read_text(encoding='utf-8'))
    seen = set()
    for row in data['boards']:
        name, target = row['name'], row['replacement']
        if not BOARD.fullmatch(name) or name in seen or (target and not BOARD.fullmatch(target)) or target == name:
            raise ValueError('Invalid board repair')
        seen.add(name)
    targets = {r['name']:r['replacement'] for r in data['boards'] if r['replacement']}
    if any(target in seen for target in targets.values()):
        raise ValueError('Repair targets must not point to audited dead boards')
    return data

def apply_audit(health, audit):
    """Apply each audit version once; never extend cooldown on every scan/restart."""
    db = health.db
    previous = db.execute("SELECT cursor FROM scan_state WHERE name='board_audit'").fetchone()
    if previous and previous[0] >= audit['version']: return
    # Discard old guesses, including same-slug matches to unrelated employers.
    with db:
        db.execute("UPDATE source_health SET replacement='' WHERE replacement != ''")
    for row in audit['boards']:
        if row['replacement']:
            health.record(row['name'],'replaced',0,row['reason'],row['replacement'])
        else:
            health.record(row['name'],'unavailable',30*86400,row['reason'])
    with db:
        db.execute("INSERT OR REPLACE INTO scan_state VALUES ('board_audit',?)",(audit['version'],))

def repaired_directory(boards, audit):
    replacements = {r['name']:r['replacement'] for r in audit['boards'] if r['replacement']}
    labels = {'ashby':'Ashby','greenhouse':'Greenhouse','lever':'Lever'}
    result = {}
    for board in boards:
        name = labels[board['vendor']]+': '+board['slug']
        target = replacements.get(name)
        if target:
            provider,slug=target.split(': ',1)
            board=dict(board,vendor=provider.lower(),slug=slug)
        result[(board['vendor'],board['slug'])]=board
    for target in replacements.values():
        provider,slug=target.split(': ',1)
        result.setdefault((provider.lower(),slug),dict(vendor=provider.lower(),slug=slug,company=slug))
    return list(result.values())
