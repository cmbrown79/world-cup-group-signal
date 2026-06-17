# World Cup Group Signal

Low-profile World Cup 2026 group-stage tracker: fixtures, scores, kickoff times, venues/cities, group standings, team and venue focus, Today rail, match drawer, and stat pulse.

## Run locally

```bash
python3 -m http.server 4173
# open http://127.0.0.1:4173
```

## Refresh data

```bash
python3 scripts/update_world_cup_tracker.py --force --notify
```

Sources:
- FIFA: scores/results
- ESPN: kickoff times, venues, cities, broadcasts
- FOX Sports: stat leader tables
