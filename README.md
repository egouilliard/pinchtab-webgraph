# PinchTab Web-Navigation Graph Crawler

Crawls a website through PinchTab (a real, JavaScript-rendered browser),
discovering pages via `<a href>` links **and** interactive widgets (buttons,
tabs, menus, accordions, SPA route changes) by actually clicking them. Builds
a navigation graph and emits:

- `<out>.json` — the graph data: `{ nodes, edges, meta }`
- `<out>.html` — a self-contained Cytoscape.js viewer (double-click to open)

Nodes are **pages** (by normalized URL) and **SPA/modal states** (same URL,
changed DOM). Edges are **links** (green) or **actions/clicks** (orange).

## Quick start

```bash
# 1. Start the isolated crawl browser (own profile/port, NOT your monday session).
#    Run this in its OWN terminal window and leave it open:
./start-crawl-browser.sh

# 2. In another terminal, crawl a site:
./run-crawl.sh https://example.com

# 3. Open the result:
xdg-open webgraph.html
```

`run-crawl.sh` forwards the auth token automatically and points at the
isolated browser on `http://localhost:9871`. You can pass any `crawl.py` flag
after the URL.

## Why a separate browser?

A "click everything" crawler must never run inside a browser holding a live
authenticated session you care about (e.g. your monday.com tab on port 9867) —
it would click through your real session. `start-crawl-browser.sh` launches a
**dedicated, isolated** PinchTab instance:

- own config (`crawl-config.json`) and profile (`.instance/profiles/crawler`)
- port `9871`, separate from your daemon on `9867`
- headless, ads/images/media blocked (fast), JS-eval enabled
- bound to localhost only

Stop it with `Ctrl-C` in its terminal.

## Useful flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--max-pages N` | 60 | hard cap on pages/nodes |
| `--interaction-depth N` | 2 | how many clicks deep to explore widgets/SPA states; `0` = links only |
| `--max-actions-per-state N` | 25 | cap on widgets probed per page/state |
| `--max-actions N` | 2000 | global budget of action clicks |
| `--allow-destructive` | off | also click logout/delete/pay/submit-style controls (**DANGER**) |
| `--include-subdomains` | off | treat `*.domain.tld` as same site |
| `--no-strip-tracking` | off | keep `utm_`/`gclid`/etc. query params |
| `--delay S` | 0.3 | DOM-settle pause after each nav/click |
| `--out NAME` | `webgraph` | output basename |

Examples:

```bash
# Static site, links only, broad:
./run-crawl.sh https://docs.example.com --interaction-depth 0 --max-pages 200 --out docs

# SPA, explore 2 clicks deep, modest page cap:
./run-crawl.sh https://app.example.com --interaction-depth 2 --max-pages 50 --out app

# Authenticated SPA: only nav controls, no data mutation, auto-recover if logged out:
./run-crawl.sh https://app.example.com --nav-only --skip-writes \
    --auth-path /auth --relogin-cmd ./login.sh --interaction-depth 1
```

Extra flags: `--nav-only` (probe only tabs/menus/role=tab|menuitem; skip bulk
table/grid buttons), `--skip-writes` (skip create/add/save/edit/etc. — map
navigation without creating data), `--auth-path /auth` + `--relogin-cmd <cmd>`
(detect session loss and re-authenticate mid-crawl).

## Finding paths between pages — `paths.py`

```bash
python3 paths.py graph.json --from /admin/users --to solviverde/documents
python3 paths.py graph.json --from A --to B --structural    # ignore sidebar/global nav
python3 paths.py graph.json --from A --to B --all --max-len 4
```

Shortest path = fewest clicks (directed BFS). `--structural` excludes the
"global nav" edges (links present on most pages, e.g. a sidebar) — useful to
see the real content structure; if it returns NO PATH, those pages are only
connected via global navigation. Also built into the viewer ("Find path
between pages").

## How-to for an action — `recipe.py`

Turns a goal into a step-by-step guide: locates the trigger button, shows the
navigation path, opens the form/modal, and reads its fields — **without
submitting** (it Escapes, so nothing is created).

```bash
./run-recipe.sh --goal "add cae" --page https://app/caes/some-project \
    --graph graph.json --start https://app/dashboard --out howto-addcae
```

Outputs: a printed how-to, `<out>.json` (machine-readable field spec:
label / type / required / options / confirm button), and `<out>.png` (the open
form). Handles both modal dialogs and navigate-to-a-form-page flows.

## Crawling sites that need login

Because the crawl browser uses a persistent profile, log in once and the
session is reused on later crawls:

1. Temporarily set `instanceDefaults.mode` to `headed` in `crawl-config.json`
   (so a window appears), start the browser, and drive it to the login page:
   `PINCHTAB_TOKEN=… pinchtab --server http://localhost:9871 nav https://app/login --new-tab`
   then fill credentials with `pinchtab … fill` / `click`, or log in by hand in
   the visible window.
2. Switch `mode` back to `headless` and run `./run-crawl.sh` as usual — the
   cookies persist in the profile.

## Safety model

- **Same-origin only** by default — won't wander off the target site.
- **Destructive-looking actions are skipped** by default and recorded as
  dashed "skipped" edges so you can see what was avoided. Override with
  `--allow-destructive` (use only on throwaway/staging accounts).
- **Hard caps** on pages, actions-per-state, interaction depth, and a global
  action budget prevent the classic SPA state explosion.

## How interaction exploration works

For each page it loads the URL and reads every link + clickable widget (stable
CSS selectors, not refs). Links become page→page edges. For each non-skipped
widget it re-materializes the state (reload + replay the click path), clicks
the widget, and classifies the result:

- **navigated** (URL changed) → page edge + enqueue the new page
- **DOM changed, same URL** → a new SPA/state node + edge, recursed into up to
  `--interaction-depth`
- **no change** → ignored

Re-materializing per probe keeps each click starting from a known state and
avoids stale element references across reloads.

## Files

- `crawl.py` — the crawler (pure Python + PinchTab CLI; no extra deps)
- `start-crawl-browser.sh` — launch the isolated crawl browser
- `run-crawl.sh` — run a crawl against it (handles the token)
- `crawl-config.json` — isolated PinchTab config (port 9871, own profile)
- `books.html` / `books.json` — example output (a crawl of books.toscrape.com)

## Importing into Neo4j (optional)

The JSON maps directly to a property graph:

```cypher
// after: WITH the json loaded as $g
UNWIND $g.nodes AS n
  MERGE (p:Page {id:n.id}) SET p.url=n.url, p.title=n.title, p.type=n.type;
UNWIND $g.edges AS e
  MATCH (a:Page {id:e.source}), (b:Page {id:e.target})
  MERGE (a)-[r:NAV {label:e.label, kind:e.kind}]->(b);
```
