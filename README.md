# Meraki-Releases

Cached Cisco Meraki firmware release notes (MX, MR, MS, CS/IOS XE, MV, MG, MT), pulled from the
[Meraki Firmware Upgrades Feed](https://community.cisco.com/t5/meraki-firmware-upgrades-feed/bg-p/networking-firmwareupgrades)
on the Cisco Community via its Khoros REST API. No browser, no login.

Why: the community pages 403 to non-browser clients and load the changelog comment via JS, so
tooling (the `meraki-troubleshoot` skill in particular) could not read them. The API can.

## Layout

```
fetch_meraki_changelogs.py   the fetcher (stdlib only, Python 3.9+)
firmware/
  index.json                 one entry per FAMILY/VERSION: release types, dates, section names, file
  posts.json                 every feed post seen, captured or pending (idempotency state)
  MX/26.1.7.json             one file per version (see schema below)
  MR/33.1.3.json
  MS/..., CS/..., MV/..., MG/..., MT/...
```

### Version file schema

| field | meaning |
|---|---|
| `family`, `version` | e.g. `MX`, `26.1.7`. Taken from the changelog title, or from the issue links when the comment is a bare table |
| `title` | the changelog heading as posted |
| `announcements[]` | every feed post for this version: `post_id`, `releaseType` (candidate, beta, stable, Recommended Release, Generally Available, legacy), `announced`, `post_url` |
| `sections` | `{heading: [lines]}` split on the headings the changelog actually uses. Names vary by family and era: `Executive summary`, `Bug fixes - general fixes`, `Known issues`, `Known issues status`, `Fixed issues`, `Release highlights`, `Legacy products notice`. Match case-insensitively on substrings |
| `text` | full changelog as plain text, one line per bullet; table rows flattened to `issue \| ID \| models \| affected versions` |
| `html` | raw comment HTML |
| `other_comments[]` | any further comments on the post (follow-up fixed-issue tables, Q&A) |

## Using it from a skill

Read `firmware/index.json` once to resolve the running version to a file, then open that file.
Raw URLs: `https://raw.githubusercontent.com/mannconsulting/Meraki-Releases/main/firmware/<FAMILY>/<VERSION>.json`.

Lookups that matter for troubleshooting:
- Running version has a known issue matching the symptom: search `sections` keys containing `known` for the model name.
- A newer version fixes the symptom: walk `index.json` for the same family with a higher version and search keys containing `fix`/`bug` for the symptom or model.
- Table-style MR notes carry an `affected versions` column per issue, so a hit there tells you exactly which trains are affected.

For a version not yet cached (feed post went up today), query the API directly:

```
https://community.cisco.com/api/2.0/search?q=SELECT id,subject,post_time FROM messages WHERE board.id='networking-firmwareupgrades' AND depth=0 ORDER BY post_time DESC LIMIT 10
https://community.cisco.com/api/2.0/search?q=SELECT body,post_time FROM messages WHERE parent.id='<post id>'
```

## Updating

```
python3 fetch_meraki_changelogs.py          # new and pending posts only, ~10 s
python3 fetch_meraki_changelogs.py --all    # walk the whole board, ~3 min
python3 fetch_meraki_changelogs.py --reparse   # no network; rebuild from stored html after a parser change
```

CMR usually posts the changelog comment within a couple of hours of FirmwareBot's announcement, so a
daily run catches nearly everything. Posts in `posts.json` with `status: pending` are re-checked each run.

Pending posts are mostly MT sensor and MG gateway announcements that never received a changelog comment.
