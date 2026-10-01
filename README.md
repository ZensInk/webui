# ZensInk WebUI

**npm:** [`@zensink/webui`](https://www.npmjs.com/package/@zensink/webui) ·
**GitHub:** [ZensInk/webui](https://github.com/ZensInk/webui) ·
**Site:** [zens.ink](https://zens.ink)

The [zens-ink](https://zens.ink) SEO toolkit as a local web console. One page,
every tool: keyword research, KD, clusters, competitor gap, site audit,
GEO/AI readiness, rank tracking — plus the 8 Pro tools behind a real
license check. The backend is the actual Python package (pure stdlib),
not a mock.

![ZensInk](https://zens.ink/og.png)

## Quick start

```bash
pip install zens-ink          # the engine (pure stdlib, no pip deps)
npx @zensink/webui           # or: npm i -g @zensink/webui && zensink-webui
```

Opens `http://127.0.0.1:8390` (auto-bumps the port if busy).

Requirements: Python 3.9+ and Node 14+. Nothing else — no build step,
no framework, no npm dependencies.

## How it finds the engine

The server looks for the `zens_ink` package in this order:

1. `ZENSINK_REPO` env var → path to a `zens-ink` clone
2. sibling directories named `zens-ink*` (a git clone next to it)
3. a normal `pip install zens-ink`

Drop a `zens-ink` clone (or `zens-ink-pro`) in the same folder and it is
picked up automatically.

## API keys

Settings → paste keys once. They land in the package `.env` and hot-apply
to every loaded tool — no restart. Free tiers: Serper (2,500/mo),
Bing Webmaster, Brave, Ahrefs free DR.

## Pro tools

Full Audit, Winability, Content Briefs, Content Radar, Competitor Radar,
AI SoV, GEO Visibility, Gap Deep — these need the paid
[zens-ink Pro](https://zens.ink/pricing) package **and** a valid license
key (Pro 1-Year or Lifetime). The key is verified against
`zens.ink/api/verify-key` and cached at `~/.zensink-webui/license.json`.
Supporter subscriptions don't include Pro tools.

Point the server at your Pro copy with `ZENSINK_PRO=/path/to/pro-scripts`.

## Env vars

| var | meaning | default |
|---|---|---|
| `ZENSINK_REPO` | path to a `zens-ink` clone | sibling discovery |
| `ZENSINK_PRO` | path to Pro `scripts/` dir | sibling discovery |
| `ZENSINK_PORT` | port | `8390` |
| `ZENSINK_PYTHON` | python executable | `python3` |

## Flags

```
zensink-webui --port 8400    # explicit port
zensink-webui --no-open      # don't open the browser
```

MIT license. The engine is developed in the open at
[ZensInk/zens-ink-seo-package](https://github.com/ZensInk/zens-ink-seo-package).
