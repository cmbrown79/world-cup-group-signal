#!/usr/bin/env python3
"""Refresh the local World Cup tracker data.

Cron behavior: exits silently outside broad match-day windows unless --force is passed.
Writes: external/ordis-bridge/public/world-cup-data.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
import unicodedata
from datetime import datetime, timedelta, time
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT
OUT = ROOT / 'world-cup-data.json'
HISTORY_OUT = ROOT / 'world-cup-history.json'
FIFA_ARTICLE = 'https://cxm-api.fifa.com/fifaplusweb/api/sections/article/S9YG2JmeGYaMUCBbm0CcD?locale=en'
ESPN_SCOREBOARD = 'https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard?limit=200&dates=20260611-20260627'
FOX_BASE = 'https://www.foxsports.com/soccer/fifa-world-cup'
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/125 Safari/537.36'
GROUP_DATES = {f'2026-06-{d:02d}' for d in range(11, 28)}

PLAYER_STAT_SPECS = [
    ('player_xg', 'Player xG', '/stats?category=standard&sort=xg&season=2026&sortOrder=desc&groupId=12', 'XG', 'Who is getting the best looks, not just who finished.'),
    ('player_goals', 'Goals', '/stats?category=standard&sort=g&season=2026&sortOrder=desc&groupId=12', 'G', 'Golden Boot pulse.'),
    ('player_assists', 'Assists', '/stats?category=standard&sort=a&season=2026&sortOrder=desc&groupId=12', 'A', 'Final-pass damage.'),
    ('player_shots', 'Shots', '/stats?category=standard&sort=s&season=2026&sortOrder=desc&groupId=12', 'S', 'Volume merchants and pressure generators.'),
    ('player_sog', 'Shots on goal', '/stats?category=standard&sort=sog&season=2026&sortOrder=desc&groupId=12', 'SOG', 'Actual keeper tests.'),
    ('player_chances', 'Chances created', '/stats?category=control&sort=cc&season=2026&sortOrder=desc&groupId=12', 'CC', 'The quiet playmaker board.'),
    ('player_rating', 'Player rating', '/stats?category=standard&sort=rtg&season=2026&sortOrder=desc&groupId=12', 'RTG', 'One-number form signal; useful but not gospel.'),
    ('keeper_save_pct', 'Keeper save %', '/stats?category=goalkeeping&sort=svpct&season=2026&sortOrder=desc&groupId=12', 'SV%', 'Shot-stopping efficiency; volatile early.'),
    ('keeper_saves', 'Keeper saves', '/stats?category=goalkeeping&sort=sv&season=2026&sortOrder=desc&groupId=12', 'SV', 'Who is under siege.'),
]

TEAM_STAT_SPECS = [
    ('team_xg', 'Team xG', '/team-stats?category=standard&sort=t_xg&season=2026&sortOrder=desc&groupId=12', 'XG', 'Best collective chance quality.'),
    ('team_goals', 'Team goals', '/team-stats?category=standard&sort=t_g&season=2026&sortOrder=desc&groupId=12', 'GF', 'Output, not projection.'),
    ('team_sog', 'Team shots on goal', '/team-stats?category=offensive&sort=t_sog&season=2026&sortOrder=desc&groupId=12', 'SOG', 'Sustained threat.'),
    ('team_chances', 'Team chances created', '/team-stats?category=offensive&sort=t_cc&season=2026&sortOrder=desc&groupId=12', 'CC', 'Chance factory board.'),
    ('team_passing', 'Passing accuracy', '/team-stats?category=offensive&sort=t_pa&season=2026&sortOrder=desc&groupId=12', 'PA', 'Control without the possession theater.'),
    ('team_tackles', 'Team tackles', '/team-stats?category=defensive&sort=t_tkl&season=2026&sortOrder=desc&groupId=12', 'TKL', 'Defensive contact / recovery activity.'),
    ('team_saves', 'Team saves', '/team-stats?category=goalkeeping&sort=t_sv&season=2026&sortOrder=desc&groupId=12', 'SV', 'Defensive stress indicator.'),
    ('team_clean_sheets', 'Clean sheets', '/team-stats?category=goalkeeping&sort=t_cs&season=2026&sortOrder=desc&groupId=12', 'CS', 'Simple defensive lock.'),
]


def get(url: str, accept='text/html') -> str:
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': accept})
    with urllib.request.urlopen(req, timeout=35) as r:
        return r.read().decode('utf-8', 'ignore')


def node_text(node) -> str:
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ''
    if node.get('nodeType') == 'text':
        return node.get('value', '')
    return ''.join(node_text(c) for c in node.get('content', []) or [])


def parse_fifa_matches():
    article = json.loads(get(FIFA_ARTICLE, 'application/json'))
    content = article['richtext']['content']
    matches = []
    current_date = None
    current_iso = None
    in_group_stage = False
    for node in content:
        txt = node_text(node).replace('\xa0', ' ').strip()
        if not txt:
            continue
        if 'FIFA World Cup 2026 Group Stage results and fixtures' in txt:
            in_group_stage = True
            continue
        if 'Round of 32' in txt:
            break
        if not in_group_stage:
            continue
        dm = re.match(r'^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+(\d{1,2})\s+June\s+2026$', txt)
        if dm:
            day = int(dm.group(2))
            current_iso = f'2026-06-{day:02d}'
            current_date = datetime(2026, 6, day).strftime('%a %d Jun')
            continue
        if not current_iso:
            continue
        for raw in [l.strip() for l in txt.splitlines() if l.strip()]:
            if 'Group ' not in raw or not re.search(r'\b(Group [A-L])\b', raw):
                continue
            m = re.match(r'^(.*?)\s+[–-]\s+Group\s+([A-L])\s+[–-]\s+(.*)$', raw)
            if not m:
                m = re.match(r'^(.*?)\s+-\s+Group\s+([A-L])\s+[–-]\s+(.*)$', raw)
            if not m:
                continue
            fixture, group, venue = m.groups()
            fixture = fixture.strip()
            venue = venue.strip()
            score = re.search(r'(.+?)\s+(\d+)\s*[-–]\s*(\d+)\s+(.+)$', fixture)
            if score:
                home, hs, away_score, away = score.groups()
                match = {'date': current_date, 'iso': current_iso, 'group': group, 'home': home.strip(), 'away': away.strip(), 'hs': int(hs), 'as': int(away_score), 'venue': venue}
            else:
                parts = re.split(r'\s+v\s+', fixture, maxsplit=1)
                if len(parts) != 2:
                    continue
                match = {'date': current_date, 'iso': current_iso, 'group': group, 'home': parts[0].strip(), 'away': parts[1].strip(), 'venue': venue}
            matches.append(match)
    if len(matches) < 72:
        raise RuntimeError(f'FIFA parse produced only {len(matches)} group matches')
    return matches[:72]


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_tr = False
        self.in_cell = False
        self.cell_tag = None
        self.current_cell = []
        self.current_row = []
        self.rows = []
    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.in_tr = True; self.current_row = []
        elif self.in_tr and tag in ('td', 'th'):
            self.in_cell = True; self.cell_tag = tag; self.current_cell = []
    def handle_endtag(self, tag):
        if self.in_cell and tag == self.cell_tag:
            text = re.sub(r'\s+', ' ', ''.join(self.current_cell)).strip()
            self.current_row.append(text)
            self.in_cell = False; self.cell_tag = None
        elif tag == 'tr' and self.in_tr:
            if self.current_row:
                self.rows.append(self.current_row)
            self.in_tr = False
    def handle_data(self, data):
        if self.in_cell:
            self.current_cell.append(data)


def clean_entity(s: str) -> str:
    return re.sub(r'\s+', ' ', unescape(s)).strip()


def split_name_team(cell: str):
    toks = cell.split()
    if toks and re.fullmatch(r'[A-Z]{2,4}', toks[-1]):
        return ' '.join(toks[:-1]), toks[-1]
    return cell, ''


def parse_stat_table(url: str, stat: str, limit=10):
    html = get(url)
    parser = TableParser(); parser.feed(html)
    rows = []
    # Data rows start with rank + name + metrics. Header row has PLAYERS/TEAMS.
    for row in parser.rows:
        if len(row) < 4 or not row[0].isdigit():
            continue
        name, team = split_name_team(clean_entity(row[1]))
        # Find value by header position if possible. On Fox pages, sorted stat has one of the cells but
        # easiest reliable value is often marked by URL sort and table order; use headers when present.
        rows.append({'rank': int(row[0]), 'name': name, 'team': team, 'raw': [clean_entity(x) for x in row[2:]]})
    # Extract headers from the first header row seen.
    headers = []
    for row in parser.rows:
        joined = ' '.join(row).upper()
        if ('PLAYERS' in joined or 'TEAMS' in joined) and stat.upper().replace('%','')[:2] in joined:
            headers = [clean_entity(x).upper() for x in row]
            break
    stat_norm = stat.upper()
    idx = None
    for i, h in enumerate(headers):
        if h == stat_norm or h.replace(' ', '') == stat_norm.replace(' ', ''):
            idx = i - 1  # header omits rank; raw cells start immediately after entity cell
            break
    out = []
    for r in rows[:limit]:
        val = None
        if idx is not None and 0 <= idx < len(r['raw']):
            val = r['raw'][idx]
        if val is None:
            # Fallback: current sort pages rank by the requested stat; pick the most plausible right-ish numeric.
            vals = [x for x in r['raw'] if x != '-']
            val = vals[-1] if vals else '-'
        out.append({'rank': r['rank'], 'name': r['name'], 'team': r['team'], 'value': val})
    return out


def fox_leader_cards():
    html = get('https://www.foxsports.com/soccer/fifa-world-cup-men/stats')
    # The SSR payload carries clean top-card metadata even if detailed API blocks direct server use.
    m = re.search(r'apiEndpointResponseData&quot;:(\{.*?\}),&quot;apiEndpointUrl', html)
    if not m:
        return []
    blob = unescape(m.group(1))
    try:
        data = json.loads(blob)
    except Exception:
        return []
    cards = []
    for section in data.get('leadersSections', []):
        for item in section.get('leaders', []):
            cards.append({
                'section': section.get('title', ''),
                'title': item.get('title', ''),
                'name': item.get('name', ''),
                'team': item.get('teamAbbreviation', ''),
                'value': item.get('statValue', ''),
                'abbr': item.get('statAbbreviation', ''),
                'selectionId': item.get('selectionId', ''),
            })
    return cards


def build_stats():
    categories = []
    for key, title, path, stat, note in PLAYER_STAT_SPECS:
        try:
            leaders = parse_stat_table(FOX_BASE + path, stat, 10)
        except Exception as e:
            leaders = []
        categories.append({'key': key, 'type': 'player', 'title': title, 'abbr': stat, 'note': note, 'leaders': leaders})
    for key, title, path, stat, note in TEAM_STAT_SPECS:
        try:
            leaders = parse_stat_table(FOX_BASE + path, stat, 10)
        except Exception:
            leaders = []
        categories.append({'key': key, 'type': 'team', 'title': title, 'abbr': stat, 'note': note, 'leaders': leaders})
    return {
        'source': 'FOX Sports stat tables; FIFA official schedule/results',
        'leaderCards': fox_leader_cards(),
        'categories': categories,
    }


def parse_espn_matches():
    data = json.loads(get(ESPN_SCOREBOARD, 'application/json'))
    events = data.get('events', [])
    matches = []
    et = ZoneInfo('America/New_York')
    for e in events:
        comp = (e.get('competitions') or [{}])[0]
        comps = comp.get('competitors', [])
        home = next((c for c in comps if c.get('homeAway') == 'home'), comps[0] if comps else {})
        away = next((c for c in comps if c.get('homeAway') == 'away'), comps[1] if len(comps) > 1 else {})
        def team(c):
            t = c.get('team', {})
            return {
                'name': t.get('displayName') or t.get('name') or '',
                'abbr': t.get('abbreviation') or '',
                'logo': t.get('logo') or '',
                'color': t.get('color') or '',
            }
        home_team, away_team = team(home), team(away)
        kickoff_iso = comp.get('date') or e.get('date')
        kickoff_dt = datetime.fromisoformat(kickoff_iso.replace('Z', '+00:00')) if kickoff_iso else None
        local_dt = kickoff_dt.astimezone(et) if kickoff_dt else None
        status = comp.get('status', {}).get('type', {})
        venue = comp.get('venue', {})
        address = venue.get('address', {}) or {}
        group = ''
        note = comp.get('altGameNote') or ''
        gm = re.search(r'Group\s+([A-L])', note)
        if gm:
            group = gm.group(1)
        m = {
            'id': e.get('id'),
            'date': local_dt.strftime('%a %d Jun') if local_dt else '',
            'iso': local_dt.date().isoformat() if local_dt else '',
            'kickoffUtc': kickoff_iso,
            'kickoffLocal': local_dt.isoformat(timespec='minutes') if local_dt else '',
            'kickoffEt': local_dt.strftime('%-I:%M %p ET') if local_dt else '',
            'group': group,
            'home': home_team['name'],
            'away': away_team['name'],
            'homeAbbr': home_team['abbr'],
            'awayAbbr': away_team['abbr'],
            'homeLogo': home_team['logo'],
            'awayLogo': away_team['logo'],
            'venue': venue.get('fullName') or '',
            'city': address.get('city') or '',
            'country': address.get('country') or '',
            'statusState': status.get('state') or '',
            'status': status.get('description') or '',
            'statusShort': status.get('shortDetail') or status.get('detail') or '',
            'completed': bool(status.get('completed')),
            'timeValid': bool(comp.get('timeValid')),
            'broadcasts': [name for b in comp.get('broadcasts', []) for name in b.get('names', [])],
            'sourceName': e.get('name') or '',
        }
        if m['completed'] or home.get('score') not in (None, '') or away.get('score') not in (None, ''):
            try:
                m['hs'] = int(home.get('score'))
                m['as'] = int(away.get('score'))
            except Exception:
                pass
        matches.append(m)
    matches.sort(key=lambda x: x.get('kickoffUtc') or '')
    if len(matches) != 72:
        raise RuntimeError(f'ESPN parse produced {len(matches)} group matches; expected 72')
    return matches


def match_team_key(name: str) -> str:
    raw = unicodedata.normalize('NFD', name or '').encode('ascii', 'ignore').decode('ascii')
    s = re.sub(r'[^a-z0-9]+', ' ', raw.lower())
    s = re.sub(r'\s+', ' ', s).strip()
    aliases = {
        'usa': 'united states',
        'united states': 'united states',
        'korea republic': 'south korea',
        'south korea': 'south korea',
        'cote d ivoire': 'ivory coast',
        'ivory coast': 'ivory coast',
        'turkiye': 'turkiye',
        'turkey': 'turkiye',
        'cabo verde': 'cape verde',
        'cape verde': 'cape verde',
        'bosnia and herzegovina': 'bosnia herzegovina',
        'bosnia herzegovina': 'bosnia herzegovina',
        'ir iran': 'iran',
        'iran': 'iran',
        'congo dr': 'dr congo',
        'dr congo': 'dr congo',
        'curacao': 'curacao',
    }
    return aliases.get(s, s)


def merge_fifa_espn_matches():
    # FIFA article is slower but currently more conservative for played/future result state.
    # ESPN gives exact kickoff, venue, city, broadcast, team metadata. Merge by team pair.
    fifa = parse_fifa_matches()
    espn = parse_espn_matches()
    by_pair = {}
    for em in espn:
        pair = frozenset([match_team_key(em.get('home')), match_team_key(em.get('away'))])
        by_pair.setdefault(pair, []).append(em)
    merged = []
    misses = []
    for fm in fifa:
        pair = frozenset([match_team_key(fm.get('home')), match_team_key(fm.get('away'))])
        candidates = by_pair.get(pair, [])
        em = candidates.pop(0) if candidates else {}
        if not em:
            misses.append(f"{fm.get('home')} v {fm.get('away')}")
        m = dict(fm)
        if em:
            m.update({
                'id': em.get('id'),
                'kickoffUtc': em.get('kickoffUtc'),
                'kickoffLocal': em.get('kickoffLocal'),
                'kickoffEt': em.get('kickoffEt'),
                'timeValid': em.get('timeValid'),
                'venueOfficial': em.get('venue') or fm.get('venue'),
                'venue': em.get('venue') or fm.get('venue'),
                'city': em.get('city') or '',
                'country': em.get('country') or '',
                'homeAbbr': em.get('homeAbbr'),
                'awayAbbr': em.get('awayAbbr'),
                'homeLogo': em.get('homeLogo'),
                'awayLogo': em.get('awayLogo'),
                'broadcasts': em.get('broadcasts') or [],
                'sourceName': em.get('sourceName') or '',
            })
        m.update({
            'statusState': 'post' if ('hs' in fm and 'as' in fm) else 'pre',
            'status': 'Full Time' if ('hs' in fm and 'as' in fm) else 'Scheduled',
            'statusShort': 'FT' if ('hs' in fm and 'as' in fm) else (m.get('kickoffEt') or 'SET'),
            'completed': bool('hs' in fm and 'as' in fm),
            'mergeNote': 'scores/results from FIFA article; kickoff/city/venue metadata from ESPN',
        })
        merged.append(m)
    if misses:
        raise RuntimeError('ESPN merge misses: ' + '; '.join(misses[:8]))
    merged.sort(key=lambda x: x.get('kickoffUtc') or x.get('iso') or '')
    return merged


def compute_standings(matches):
    tables = {g: {} for g in 'ABCDEFGHIJKL'}
    for m in matches:
        g = m.get('group') or ''
        if g not in tables:
            continue
        for team in (m['home'], m['away']):
            tables[g].setdefault(team, {'team': team, 'p': 0, 'w': 0, 'd': 0, 'l': 0, 'gf': 0, 'ga': 0, 'gd': 0, 'pts': 0})
        if not (isinstance(m.get('hs'), int) and isinstance(m.get('as'), int)):
            continue
        h, a, hs, av = m['home'], m['away'], m['hs'], m['as']
        tables[g][h]['p'] += 1; tables[g][a]['p'] += 1
        tables[g][h]['gf'] += hs; tables[g][h]['ga'] += av
        tables[g][a]['gf'] += av; tables[g][a]['ga'] += hs
        if hs > av:
            tables[g][h]['w'] += 1; tables[g][h]['pts'] += 3; tables[g][a]['l'] += 1
        elif hs < av:
            tables[g][a]['w'] += 1; tables[g][a]['pts'] += 3; tables[g][h]['l'] += 1
        else:
            tables[g][h]['d'] += 1; tables[g][a]['d'] += 1; tables[g][h]['pts'] += 1; tables[g][a]['pts'] += 1
    out = {}
    for g, rows in tables.items():
        ranked = []
        for r in rows.values():
            r['gd'] = r['gf'] - r['ga']
            ranked.append(r)
        ranked.sort(key=lambda r: (-r['pts'], -r['gd'], -r['gf'], r['team']))
        for i, r in enumerate(ranked, 1):
            r['rank'] = i
            # lightweight status — exact tiebreak clinch math comes later
            remaining = 3 - r['p']
            r['status'] = 'qualified-watch' if r['pts'] >= 6 else ('danger' if remaining == 0 and r['pts'] <= 2 else 'alive')
        out[g] = ranked
    return out


def validate_payload(matches):
    warnings = []
    if len(matches) != 72:
        warnings.append(f'expected 72 group matches, got {len(matches)}')
    keys = [(m.get('home'), m.get('away'), m.get('kickoffUtc')) for m in matches]
    if len(set(keys)) != len(keys):
        warnings.append('duplicate match identity detected')
    if any(not m.get('group') for m in matches):
        warnings.append('one or more matches missing group')
    if any(not m.get('kickoffUtc') for m in matches):
        warnings.append('one or more matches missing kickoff time')
    return warnings


def stat_snapshot(payload):
    return {
        'generatedAt': payload.get('generatedAt'),
        'played': payload.get('summary', {}).get('played'),
        'matches': [
            {
                'id': m.get('id'), 'group': m.get('group'), 'home': m.get('home'), 'away': m.get('away'),
                'hs': m.get('hs'), 'as': m.get('as'), 'completed': bool('hs' in m and 'as' in m),
                'kickoffUtc': m.get('kickoffUtc'), 'venue': m.get('venue'), 'city': m.get('city')
            }
            for m in payload.get('matches', [])
        ],
        'standings': payload.get('standings', {}),
        'stats': [
            {
                'title': c.get('title'), 'abbr': c.get('abbr'), 'type': c.get('type'),
                'leaders': [
                    {'rank': r.get('rank'), 'name': r.get('name'), 'team': r.get('team'), 'value': r.get('value')}
                    for r in (c.get('leaders') or [])[:10]
                ]
            }
            for c in payload.get('stats', {}).get('categories', [])
        ]
    }


def merge_history(payload):
    current = stat_snapshot(payload)
    history = []
    if HISTORY_OUT.exists():
        try:
            raw = json.loads(HISTORY_OUT.read_text())
            history = raw.get('snapshots', []) if isinstance(raw, dict) else []
        except Exception:
            history = []
    # Seed from previous payload if no history exists; gives the motion lab a first baseline.
    if not history and OUT.exists():
        try:
            prev = json.loads(OUT.read_text())
            if prev.get('generatedAt') and prev.get('generatedAt') != payload.get('generatedAt'):
                history.append(stat_snapshot(prev))
        except Exception:
            pass
    by_time = {h.get('generatedAt'): h for h in history if h.get('generatedAt')}
    by_time[current['generatedAt']] = current
    snapshots = sorted(by_time.values(), key=lambda x: x.get('generatedAt') or '')[-96:]
    return {
        'generatedAt': payload.get('generatedAt'),
        'source': 'world-cup-data.json refresh snapshots',
        'snapshotCount': len(snapshots),
        'snapshots': snapshots,
    }


def inside_window(now: datetime) -> bool:
    # Exact-ish match window from ESPN kickoff times: refresh from 90m before first kickoff
    # through 3h after last kickoff on each group-stage matchday. Fallback to broad date gate.
    try:
        matches = parse_espn_matches()
        et = ZoneInfo('America/New_York')
        for m in matches:
            if not m.get('kickoffUtc'):
                continue
            ko = datetime.fromisoformat(m['kickoffUtc'].replace('Z', '+00:00')).astimezone(et)
            if ko - timedelta(minutes=90) <= now <= ko + timedelta(hours=3):
                return True
        return False
    except Exception:
        if now.date().isoformat() not in GROUP_DATES:
            return False
        return time(7, 0) <= now.time() <= time(23, 59, 59)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--notify', action='store_true', help='Print a one-line update when the data changes. Cron leaves this off to stay silent.')
    args = ap.parse_args()
    now = datetime.now(ZoneInfo('America/New_York'))
    if not args.force and not inside_window(now):
        return 0
    old = OUT.read_text() if OUT.exists() else ''
    matches = merge_fifa_espn_matches()
    played = sum(1 for m in matches if 'hs' in m and 'as' in m)
    warnings = validate_payload(matches)
    payload = {
        'generatedAt': now.isoformat(timespec='seconds'),
        'refreshPolicy': 'Cron checks every 30 minutes and writes only inside ESPN kickoff windows: 90m pre-match through 3h post-match; manual force bypasses the gate.',
        'scheduleSource': FIFA_ARTICLE,
        'timeVenueSource': ESPN_SCOREBOARD,
        'statsSource': 'https://www.foxsports.com/soccer/fifa-world-cup-men/stats',
        'matches': matches,
        'standings': compute_standings(matches),
        'validation': {'ok': not warnings, 'warnings': warnings},
        'stats': build_stats(),
        'summary': {'matches': len(matches), 'played': played, 'scheduled': len(matches) - played, 'groups': 12, 'venues': len(set(m.get('venue') for m in matches if m.get('venue')))},
    }
    history_payload = merge_history(payload)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + '\n'
    history_text = json.dumps(history_payload, ensure_ascii=False, indent=2) + '\n'
    OUT.write_text(text)
    HISTORY_OUT.write_text(history_text)
    if args.notify and text != old:
        print(f'world-cup-tracker refreshed: {played}/{len(matches)} played, {len(payload["stats"]["categories"])} stat boards')
    return 0

if __name__ == '__main__':
    sys.exit(main())
