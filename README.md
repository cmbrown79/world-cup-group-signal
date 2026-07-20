# World Cup Group Signal

> **Archived event baseline — July 2026.** The World Cup 2026 tracker is frozen after the event and retained as a reusable static-data instrument. The final event state is tagged `world-cup-2026-final`; scheduled refresh is disabled.

Low-profile World Cup 2026 group-stage tracker: fixtures, scores, kickoff times, venues/cities, group standings, team and venue focus, Today rail, match drawer, and stat pulse.

## Reuse for another event

1. Unarchive or fork the repository; do not overwrite the final 2026 tag.
2. Update the event identity, source adapters, schedule model, and venue/team schema.
3. Run a forced local refresh and verify the generated JSON against a concrete public result.
4. Enable and exercise the manual `Refresh World Cup data` workflow as the restart canary.
5. Re-enable scheduled refresh only after deployment, negative-path, and hosted-data verification pass.

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
