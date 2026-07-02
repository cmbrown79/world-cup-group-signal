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
RECON_OUT = ROOT / 'world-cup-reconstructed-history.json'
FIFA_ARTICLE = 'https://cxm-api.fifa.com/fifaplusweb/api/sections/article/S9YG2JmeGYaMUCBbm0CcD?locale=en'
ESPN_SCOREBOARD = 'https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard?limit=200&dates=20260611-20260627'
ESPN_KNOCKOUT_SCOREBOARD = 'https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard?limit=200&dates=20260628-20260719'
ESPN_SUMMARY = 'https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/summary?event={event_id}'
FOX_BASE = 'https://www.foxsports.com/soccer/fifa-world-cup'
XGSCORE_TEAM_STATS = 'https://api.xgscore.io/team-stats/current?tournamentSlug=world-cup'
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/125 Safari/537.36'
GROUP_DATES = {f'2026-06-{d:02d}' for d in range(11, 28)}
EVENT_DATES = GROUP_DATES | {f'2026-06-{d:02d}' for d in range(28, 31)} | {f'2026-07-{d:02d}' for d in range(1, 20)}

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



def parse_fifa_knockout_matches():
    """Parse FIFA's knockout schedule/results from the authoritative schedule article."""
    article = json.loads(get(FIFA_ARTICLE, 'application/json'))
    content = article['richtext']['content']
    matches = []
    current_date = None
    current_iso = None
    current_round = None
    month_nums = {'June': 6, 'July': 7}
    round_map = [
        ('Round of 32', 'Round of 32'),
        ('Round of 16', 'Round of 16'),
        ('quarter-final', 'Quarter-finals'),
        ('semi-final', 'Semi-finals'),
        ('bronze final', 'Bronze final'),
        ('Final', 'Final'),
    ]
    for node in content:
        txt = node_text(node).replace('\xa0', ' ').strip()
        if not txt:
            continue
        for raw in [l.strip() for l in txt.splitlines() if l.strip()]:
            if 'Group Stage results' in raw:
                current_round = None
            for marker, label in round_map:
                if marker in raw:
                    current_round = label
                    if label == 'Final' and 'bronze' in raw.lower():
                        current_round = 'Bronze final'
                    break
            dm = re.match(r'^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+(\d{1,2})\s+(June|July)\s+2026$', raw)
            if dm:
                day = int(dm.group(2)); month = month_nums[dm.group(3)]
                current_iso = f'2026-{month:02d}-{day:02d}'
                current_date = datetime(2026, month, day).strftime('%a %d %b')
                continue
            if not current_round or not current_iso or not raw.startswith('Match '):
                continue
            mm = re.match(r'^Match\s+(\d+)\s+[–-]\s+(.+)$', raw)
            if not mm:
                continue
            match_no = int(mm.group(1)); rest = mm.group(2).strip()
            parts = [x.strip() for x in re.split(r'\s+[–-]\s+|\s+-\s+', rest) if x.strip()]
            if len(parts) < 2:
                continue
            venue = parts[-1]
            time_et = None
            fixture = ' - '.join(parts[:-1])
            if len(parts) >= 3 and re.fullmatch(r'\d{1,2}:\d{2}', parts[-2]):
                time_et = parts[-2]
                fixture = ' - '.join(parts[:-2])
            match = {'matchNo': match_no, 'round': current_round, 'date': current_date, 'iso': current_iso, 'venue': venue}
            if time_et:
                # FIFA lists all kickoffs in ET. Keep the literal source time for schedule display.
                match['kickoffEt'] = datetime.strptime(time_et, '%H:%M').strftime('%-I:%M %p ET')
                match['sourceKickoffEt'] = time_et
            score = re.match(r'(.+?)\s+(\d+)\s*[-–]\s*(\d+)\s+(.+?)(?:\s+\((AET|PSO\s+(\d+)\s*[-–]\s*(\d+))\))?$', fixture)
            if score:
                home, hs, away_score, away, extra, pso_h, pso_a = score.groups()
                match.update({'home': home.strip(), 'away': away.strip(), 'hs': int(hs), 'as': int(away_score), 'completed': True})
                if extra == 'AET':
                    match['statusShort'] = 'AET'
                if pso_h is not None and pso_a is not None:
                    match.update({'homePso': int(pso_h), 'awayPso': int(pso_a), 'statusShort': 'FT-Pens'})
            else:
                sides = re.split(r'\s+v\s+', fixture, maxsplit=1)
                if len(sides) == 2:
                    match.update({'home': sides[0].strip(), 'away': sides[1].strip(), 'completed': False})
            if 'home' in match and 'away' in match:
                match.setdefault('statusState', 'post' if match.get('completed') else 'pre')
                match.setdefault('status', 'Full Time' if match.get('completed') else 'Scheduled')
                match.setdefault('statusShort', 'FT' if match.get('completed') else match.get('kickoffEt', 'SET'))
                matches.append(match)
    if len(matches) != 32:
        raise RuntimeError(f'FIFA knockout parse produced {len(matches)} matches; expected 32')
    return matches


def parse_espn_knockout_matches():
    data = json.loads(get(ESPN_KNOCKOUT_SCOREBOARD, 'application/json'))
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
            return {'name': t.get('displayName') or t.get('name') or '', 'abbr': t.get('abbreviation') or '', 'logo': t.get('logo') or ''}
        home_team, away_team = team(home), team(away)
        kickoff_iso = comp.get('date') or e.get('date')
        kickoff_dt = datetime.fromisoformat(kickoff_iso.replace('Z', '+00:00')) if kickoff_iso else None
        local_dt = kickoff_dt.astimezone(et) if kickoff_dt else None
        status = comp.get('status', {}).get('type', {})
        venue = comp.get('venue', {})
        address = venue.get('address', {}) or {}
        m = {
            'id': e.get('id'), 'date': local_dt.strftime('%a %d %b') if local_dt else '', 'iso': local_dt.date().isoformat() if local_dt else '',
            'kickoffUtc': kickoff_iso, 'kickoffLocal': local_dt.isoformat(timespec='minutes') if local_dt else '', 'kickoffEt': local_dt.strftime('%-I:%M %p ET') if local_dt else '',
            'home': home_team['name'], 'away': away_team['name'], 'homeAbbr': home_team['abbr'], 'awayAbbr': away_team['abbr'], 'homeLogo': home_team['logo'], 'awayLogo': away_team['logo'],
            'venue': venue.get('fullName') or '', 'city': address.get('city') or '', 'country': address.get('country') or '',
            'statusState': status.get('state') or '', 'status': status.get('description') or '', 'statusShort': status.get('shortDetail') or status.get('detail') or '',
            'completed': bool(status.get('completed')), 'timeValid': bool(comp.get('timeValid')), 'broadcasts': [name for b in comp.get('broadcasts', []) for name in b.get('names', [])],
            'sourceName': e.get('name') or '', 'round': (comp.get('altGameNote') or '').replace('FIFA World Cup, ', ''),
            'notes': [n.get('headline') or n.get('text') for n in (comp.get('notes') or []) if n.get('headline') or n.get('text')],
        }
        if m['completed'] or status.get('state') == 'in':
            try:
                m['hs'] = int(home.get('score')); m['as'] = int(away.get('score'))
            except Exception:
                pass
        matches.append(m)
    matches.sort(key=lambda x: x.get('kickoffUtc') or '')
    return matches


def merge_fifa_espn_knockout():
    fifa = parse_fifa_knockout_matches()
    espn = parse_espn_knockout_matches()
    by_pair = {}
    for em in espn:
        pair = frozenset([match_team_key(em.get('home')), match_team_key(em.get('away'))])
        by_pair.setdefault(pair, []).append(em)
    by_round_order = {}
    for em in espn:
        by_round_order.setdefault(em.get('round') or '', []).append(em)
    used_ids = set()
    merged = []
    for idx, fm in enumerate(fifa):
        m = dict(fm)
        em = None
        # Known teams: pair merge is safest across source ordering.
        if 'Winner match' not in fm.get('home', '') and 'Winner match' not in fm.get('away', '') and 'Runner-up match' not in fm.get('home', ''):
            pair = frozenset([match_team_key(fm.get('home')), match_team_key(fm.get('away'))])
            cands = [x for x in by_pair.get(pair, []) if x.get('id') not in used_ids]
            if cands:
                em = cands[0]
        if em is None and ('Winner match' in fm.get('home', '') or 'Winner match' in fm.get('away', '') or 'Runner-up match' in fm.get('home', '')):
            cands = by_round_order.get(fm.get('round'), [])
            # Use same ordinal within round for future placeholder matches. Do not reindex after used IDs;
            # otherwise an already-played pair miss can steal a later scheduled match. Tiny footgun, large clown shoes.
            same_round_prior = len([x for x in fifa[:idx] if x.get('round') == fm.get('round')])
            if same_round_prior < len(cands) and cands[same_round_prior].get('id') not in used_ids:
                em = cands[same_round_prior]
        if em:
            used_ids.add(em.get('id'))
            m.update({
                'id': em.get('id'), 'kickoffUtc': em.get('kickoffUtc'), 'kickoffLocal': em.get('kickoffLocal'), 'kickoffEt': em.get('kickoffEt') or m.get('kickoffEt'),
                'timeValid': em.get('timeValid'), 'venueOfficial': em.get('venue') or fm.get('venue'), 'venue': em.get('venue') or fm.get('venue'), 'city': em.get('city') or '', 'country': em.get('country') or '',
                'homeAbbr': em.get('homeAbbr'), 'awayAbbr': em.get('awayAbbr'), 'homeLogo': em.get('homeLogo'), 'awayLogo': em.get('awayLogo'), 'broadcasts': em.get('broadcasts') or [],
                'sourceName': em.get('sourceName') or '', 'notes': em.get('notes') or [],
            })
            if em.get('completed') or em.get('statusState') == 'in':
                if 'hs' in em and 'as' in em:
                    m['hs'] = em['hs']; m['as'] = em['as']
            completed = bool(m.get('completed') or em.get('completed'))
            scored = isinstance(m.get('hs'), int) and isinstance(m.get('as'), int)
            is_live = bool(em.get('statusState') == 'in')
            m.update({'statusState': 'post' if completed else ('in' if is_live else 'pre'), 'status': 'Full Time' if completed else (em.get('status') or 'Scheduled'), 'statusShort': em.get('statusShort') or m.get('statusShort') or ('FT' if completed else 'SET'), 'completed': completed and scored})
        m['winner'] = knockout_winner(m)
        merged.append(m)
    return merged


def knockout_winner(m):
    if not (isinstance(m.get('hs'), int) and isinstance(m.get('as'), int)):
        return ''
    if m['hs'] > m['as']:
        return m.get('home', '')
    if m['as'] > m['hs']:
        return m.get('away', '')
    if isinstance(m.get('homePso'), int) and isinstance(m.get('awayPso'), int):
        return m.get('home', '') if m['homePso'] > m['awayPso'] else m.get('away', '')
    note = ' '.join(m.get('notes') or [])
    for side in [m.get('home',''), m.get('away','')]:
        if side and match_team_key(side) in match_team_key(note):
            return side
    return ''

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


def fmt_float(v, places=2):
    try:
        return f'{float(v):.{places}f}'.rstrip('0').rstrip('.')
    except Exception:
        return str(v)


def fetch_xgscore_team_stats():
    req = urllib.request.Request(XGSCORE_TEAM_STATS, headers={
        'User-Agent': UA,
        'Accept': 'application/json',
        'Origin': 'https://xgscore.io',
        'Referer': 'https://xgscore.io/xg-statistics/world-cup/2026',
    })
    with urllib.request.urlopen(req, timeout=35) as r:
        rows = json.loads(r.read().decode('utf-8', 'ignore'))
    out = []
    for row in rows:
        team = (row.get('team') or {}).get('name') or ''
        if not team:
            continue
        row = dict(row)
        display_aliases = {
            'Bosnia and Herz.': 'Bosnia and Herzegovina',
            'Czech': 'Czechia',
            'Saudi A.': 'Saudi Arabia',
            'South Korea': 'Korea Republic',
            'Turkey': 'Türkiye',
        }
        team = display_aliases.get(team, team)
        row['teamName'] = team
        row['teamKey'] = match_team_key(team)
        out.append(row)
    return out


def xgscore_category(rows, key, title, field, abbr, note, reverse=True, places=2, transform=None):
    vals = []
    for row in rows:
        val = row.get(field)
        if transform:
            val = transform(row)
        if val is None:
            continue
        try:
            fval = float(val)
        except Exception:
            continue
        vals.append((row['teamName'], fval))
    vals.sort(key=lambda x: ((-x[1]) if reverse else x[1], x[0]))
    leaders = [{'rank': i, 'name': name, 'team': 'team', 'value': fmt_float(val, places)} for i, (name, val) in enumerate(vals[:10], 1)]
    return {'key': key, 'type': 'team', 'title': title, 'abbr': abbr, 'note': note, 'leaders': leaders, 'source': 'xGscore'}


def build_xgscore_categories():
    rows = fetch_xgscore_team_stats()
    cats = [
        xgscore_category(rows, 'xgs_team_xg', 'Team xG', 'xgScored', 'XG', 'xGscore team expected goals; stronger advanced substrate than FOX aggregate table.'),
        xgscore_category(rows, 'xgs_team_xga', 'Team xG conceded', 'xgConceded', 'xGA', 'Chance quality allowed. Lower is better.', reverse=False),
        xgscore_category(rows, 'xgs_team_xgot', 'Team xG on target', 'xgOnTarget', 'xGOT', 'Shot quality after placement: keeper-test danger.'),
        xgscore_category(rows, 'xgs_team_xpoints', 'Team xPoints', 'xPoints', 'xPTS', 'Expected points from chance profile.'),
        xgscore_category(rows, 'xgs_team_open_xg', 'Open-play xG', 'xgOpenPlay', 'OPXG', 'Threat generated from open play.'),
        xgscore_category(rows, 'xgs_team_set_xg', 'Set-play xG', 'xgSetPlay', 'SPXG', 'Threat generated from set pieces.'),
        xgscore_category(rows, 'xgs_team_finish_delta', 'Finishing delta', None, 'G-XG', 'Goals minus xG: hot finishing or cold finishing.', transform=lambda r: (r.get('goalsScored') or 0) - (r.get('xgScored') or 0)),
    ]
    return rows, cats


def build_stats():
    categories = []
    xgscore_rows = []
    xgscore_cats = []
    try:
        xgscore_rows, xgscore_cats = build_xgscore_categories()
    except Exception:
        xgscore_rows, xgscore_cats = [], []
    for key, title, path, stat, note in PLAYER_STAT_SPECS:
        try:
            leaders = parse_stat_table(FOX_BASE + path, stat, 10)
        except Exception as e:
            leaders = []
        categories.append({'key': key, 'type': 'player', 'title': title, 'abbr': stat, 'note': note, 'leaders': leaders})
    xgscore_by_title = {c['title']: c for c in xgscore_cats}
    for key, title, path, stat, note in TEAM_STAT_SPECS:
        if title in xgscore_by_title:
            categories.append(xgscore_by_title.pop(title))
            continue
        try:
            leaders = parse_stat_table(FOX_BASE + path, stat, 10)
        except Exception:
            leaders = []
        categories.append({'key': key, 'type': 'team', 'title': title, 'abbr': stat, 'note': note, 'leaders': leaders})
    categories.extend(xgscore_by_title.values())
    return {
        'source': 'FOX Sports player/stat tables + xGscore team advanced stats + FIFA/ESPN schedule/results',
        'advancedTeamStatsSource': XGSCORE_TEAM_STATS,
        'xgscoreTeamStats': xgscore_rows,
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
        'cote divoire': 'ivory coast',
        'ivory coast': 'ivory coast',
        'turkiye': 'turkiye',
        'turkey': 'turkiye',
        'cabo verde': 'cape verde',
        'cape verde': 'cape verde',
        'bosnia and herzegovina': 'bosnia herzegovina',
        'bosnia herzegovina': 'bosnia herzegovina',
        'bosnia herzegovina': 'bosnia herzegovina',
        'bosnia-herzegovina': 'bosnia herzegovina',
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
        fifa_scored = ('hs' in fm and 'as' in fm)
        espn_scored = em and ('hs' in em and 'as' in em) and (em.get('completed') or em.get('statusState') == 'in')
        if (not fifa_scored) and espn_scored:
            m['hs'] = em.get('hs')
            m['as'] = em.get('as')
        scored = isinstance(m.get('hs'), int) and isinstance(m.get('as'), int)
        espn_state = em.get('statusState') if em else ''
        espn_status = em.get('status') if em else ''
        espn_short = em.get('statusShort') if em else ''
        completed = bool(fifa_scored or (em and em.get('completed')))
        is_live = bool(espn_state == 'in' or (espn_short and espn_short not in ('FT', 'Scheduled') and scored and not completed))
        m.update({
            'statusState': 'post' if completed else ('in' if is_live else 'pre'),
            'status': 'Full Time' if completed else (espn_status or 'Scheduled'),
            'statusShort': 'FT' if completed else (espn_short or (m.get('kickoffEt') or 'SET')),
            'completed': completed,
            'mergeNote': 'scores/results from FIFA when available; ESPN supplies live/HT/FT state, fallback scores, kickoff/city/venue metadata',
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


def parse_num(v):
    try:
        return float(str(v).replace('%','').replace(',','').strip())
    except Exception:
        return 0.0


def add_stat(board, title, abbr, typ, name, team, value):
    if not name:
        return
    k = (title, name, team or '')
    board[k] = board.get(k, 0.0) + value


def ranked_from_board(board, title, abbr, typ, note, value_fmt=None, limit=10):
    rows = [(name, team, val) for (t, name, team), val in board.items() if t == title]
    rows.sort(key=lambda x: (-x[2], x[0]))
    leaders = []
    for i, (name, team, val) in enumerate(rows[:limit], 1):
        if value_fmt:
            value = value_fmt(val)
        else:
            value = str(int(val)) if abs(val - int(val)) < 1e-9 else f'{val:.2f}'.rstrip('0').rstrip('.')
        leaders.append({'rank': i, 'name': name, 'team': team, 'value': value})
    return {'title': title, 'abbr': abbr, 'type': typ, 'note': note, 'leaders': leaders}


def parse_goal_text(text):
    # ESPN key event prose is regular enough: "Name (Team) ... Assisted by Name."
    if not text or 'Goal!' not in text:
        return None, None
    try:
        after = text.split('. ', 1)[1]
    except Exception:
        after = text
    scorer = after.split(' (', 1)[0].strip()
    assist = None
    if 'Assisted by ' in text:
        assist = text.split('Assisted by ', 1)[1].split('.', 1)[0].strip()
        for cut in [' with ', ' following ', ' from ', ' after ']:
            if cut in assist:
                assist = assist.split(cut, 1)[0].strip()
    return scorer, assist


def reconstruct_history(payload):
    completed = [m for m in payload.get('matches', []) if m.get('completed') or ('hs' in m and 'as' in m)]
    completed.sort(key=lambda m: (m.get('kickoffUtc') or '', m.get('id') or ''))
    board = {}
    snapshots = []
    team_totals = {}
    for m in completed:
        try:
            summary = json.loads(get(ESPN_SUMMARY.format(event_id=m['id']), 'application/json'))
        except Exception:
            continue
        # Team boxscore stats.
        for t in summary.get('boxscore', {}).get('teams', []):
            team = t.get('team', {}).get('displayName')
            stats = {x.get('name'): parse_num(x.get('displayValue')) for x in t.get('statistics', [])}
            if not team:
                continue
            team_totals.setdefault(team, {'accuratePasses': 0, 'totalPasses': 0})
            gf = m.get('hs') if team == m.get('home') else m.get('as') if team == m.get('away') else 0
            ga = m.get('as') if team == m.get('home') else m.get('hs') if team == m.get('away') else 0
            add_stat(board, 'Team goals', 'GF', 'team', team, 'team', float(gf or 0))
            add_stat(board, 'Team shots on goal', 'SOG', 'team', team, 'team', stats.get('shotsOnTarget', 0))
            add_stat(board, 'Team saves', 'SV', 'team', team, 'team', stats.get('saves', 0))
            add_stat(board, 'Team shots', 'S', 'team', team, 'team', stats.get('totalShots', 0))
            add_stat(board, 'Clean sheets', 'CS', 'team', team, 'team', 1.0 if ga == 0 else 0.0)
            add_stat(board, 'Team defensive interventions', 'DI', 'team', team, 'team', stats.get('defensiveInterventions', 0))
            team_totals[team]['accuratePasses'] += stats.get('accuratePasses', 0)
            team_totals[team]['totalPasses'] += stats.get('totalPasses', 0)
        # ESPN per-match leaders for shots/saves/passes/defensive interventions.
        for team_block in summary.get('leaders', []):
            team = team_block.get('team', {}).get('abbreviation') or team_block.get('team', {}).get('displayName') or ''
            for grp in team_block.get('leaders', []):
                title = grp.get('name')
                for row in grp.get('leaders', []):
                    name = row.get('athlete', {}).get('displayName')
                    val = parse_num(row.get('displayValue'))
                    if title == 'totalShots': add_stat(board, 'Shots', 'S', 'player', name, team, val)
                    elif title == 'saves': add_stat(board, 'Keeper saves', 'SV', 'player', name, team, val)
                    elif title == 'accuratePasses': add_stat(board, 'Accurate passes', 'AP', 'player', name, team, val)
                    elif title == 'defensiveInterventions': add_stat(board, 'Defensive interventions', 'DI', 'player', name, team, val)
        # Goals and assists from goal event prose.
        for ev in summary.get('keyEvents', []):
            if ev.get('type', {}).get('type') == 'goal':
                team = (ev.get('team') or {}).get('displayName') or ''
                scorer, assist = parse_goal_text(ev.get('text'))
                add_stat(board, 'Goals', 'G', 'player', scorer, team, 1)
                if assist: add_stat(board, 'Assists', 'A', 'player', assist, team, 1)
        # Passing accuracy is weighted from accumulated passes.
        pass_board = {}
        for team, vals in team_totals.items():
            total = vals.get('totalPasses') or 0
            if total:
                pass_board[('Passing accuracy', team, 'team')] = vals.get('accuratePasses', 0) / total
        merged = dict(board); merged.update(pass_board)
        cats = [
            ranked_from_board(merged, 'Goals', 'G', 'player', 'Reconstructed from ESPN goal events.'),
            ranked_from_board(merged, 'Assists', 'A', 'player', 'Reconstructed from ESPN goal-event assists.'),
            ranked_from_board(merged, 'Shots', 'S', 'player', 'Reconstructed from ESPN match leaders.'),
            ranked_from_board(merged, 'Keeper saves', 'SV', 'player', 'Reconstructed from ESPN match leaders.'),
            ranked_from_board(merged, 'Team goals', 'GF', 'team', 'Reconstructed from official scorelines.'),
            ranked_from_board(merged, 'Team shots on goal', 'SOG', 'team', 'Reconstructed from ESPN team boxscores.'),
            ranked_from_board(merged, 'Team saves', 'SV', 'team', 'Reconstructed from ESPN team boxscores.'),
            ranked_from_board(merged, 'Team shots', 'S', 'team', 'Reconstructed from ESPN team boxscores.'),
            ranked_from_board(merged, 'Clean sheets', 'CS', 'team', 'Reconstructed from official scorelines.'),
            ranked_from_board(merged, 'Passing accuracy', 'PA', 'team', 'Weighted from ESPN accurate/total passes.', lambda v: f'{v:.2f}'),
        ]
        snapshots.append({
            'generatedAt': m.get('kickoffUtc') or payload.get('generatedAt'),
            'played': len(snapshots) + 1,
            'lastMatchId': m.get('id'),
            'lastMatch': f"{m.get('home')} {m.get('hs')}–{m.get('as')} {m.get('away')}",
            'stats': cats,
        })
    return {'generatedAt': payload.get('generatedAt'), 'source': 'Reconstructed from ESPN per-match summaries/boxscores plus official scorelines; xG/chances/rating still require a match-level source.', 'coverage': {'reconstructedMatches': len(snapshots), 'completedMatches': len(completed)}, 'snapshots': snapshots}


def inside_window(now: datetime) -> bool:
    # During the group stage, refresh on every scheduled tick. Earlier we limited writes
    # to 90m pre-match through 3h post-kickoff, but midnight ET / west-coast fixtures
    # plus upstream result lag meant completed matches could remain stale until the next
    # live window. The payload is cheap enough to rebuild every 30m during group dates.
    if now.date().isoformat() in EVENT_DATES:
        return True
    # One-day grace catches final late-night result/stat corrections after the final.
    if now.date().isoformat() == '2026-07-20':
        return True
    return False


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
    knockout = merge_fifa_espn_knockout()
    played = sum(1 for m in matches if 'hs' in m and 'as' in m)
    knockout_played = sum(1 for m in knockout if m.get('completed') or ('hs' in m and 'as' in m))
    warnings = validate_payload(matches)
    if len(knockout) != 32:
        warnings.append(f'expected 32 knockout matches, got {len(knockout)}')
    payload = {
        'generatedAt': now.isoformat(timespec='seconds'),
        'refreshPolicy': 'During group-stage dates, scheduled refresh rebuilds every 30 minutes; manual force also supported. Page auto-refetches the published JSON every 2 minutes.',
        'scheduleSource': FIFA_ARTICLE,
        'timeVenueSource': ESPN_SCOREBOARD,
        'statsSource': 'FOX Sports player boards + xGscore team advanced stats',
        'advancedTeamStatsSource': XGSCORE_TEAM_STATS,
        'matches': matches,
        'knockout': knockout,
        'standings': compute_standings(matches),
        'validation': {'ok': not warnings, 'warnings': warnings},
        'stats': build_stats(),
        'summary': {'matches': len(matches), 'played': played, 'scheduled': len(matches) - played, 'groups': 12, 'venues': len(set(m.get('venue') for m in matches if m.get('venue'))), 'knockoutMatches': len(knockout), 'knockoutPlayed': knockout_played, 'knockoutScheduled': len(knockout) - knockout_played},
    }
    history_payload = merge_history(payload)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + '\n'
    history_text = json.dumps(history_payload, ensure_ascii=False, indent=2) + '\n'
    OUT.write_text(text)
    HISTORY_OUT.write_text(history_text)
    RECON_OUT.write_text(json.dumps(reconstruct_history(payload), ensure_ascii=False, indent=2) + '\n')
    if args.notify and text != old:
        print(f'world-cup-tracker refreshed: groups {played}/{len(matches)} played, knockouts {knockout_played}/{len(knockout)}, {len(payload["stats"]["categories"])} stat boards')
    return 0

if __name__ == '__main__':
    sys.exit(main())
