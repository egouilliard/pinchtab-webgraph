# Live end-to-end test: `crawl → howto → perform`

This is the real-browser proof that the runnable-command layer works: crawl a site
through the live PinchTab browser, discover its download/form affordances, then
**actually perform** the action (navigate the path and let the browser download the file
/ fill the form). It uses a tiny **local** site so it needs no external network.

Everything below was executed and observed — the two bugs it caught (and their fixes)
are noted at the end.

## 1. A local test site (home → reports → team)

`reports.html` has a **direct** download link and a **JS-triggered** export button;
`team.html` has a create-form:

```html
<!-- reports.html -->
<a id="dl-q3" href="/files/q3-report.pdf" download>Download Q3 report</a>
<button id="export-csv" onclick="exportCsv()">Export CSV</button>   <!-- Blob → a.click() -->

<!-- team.html -->
<form id="create-member">
  <input id="mname" type="text" required>
  <select id="mrole"><option>Viewer</option><option>Editor</option><option>Admin</option></select>
  <button id="save-member" type="submit">Create member</button>
</form>
```

Serve it on localhost:

```bash
python3 -m http.server 8099 --bind 127.0.0.1 --directory ./site
```

## 2. A bridge with downloads enabled

The crawl bridge ships `security.allowDownload = false` (discovery is read-only). For a
`perform` that downloads, run a bridge whose config sets `allowDownload: true` (and
`allowUpload: true` if you'll upload), then:

```bash
PINCHTAB_CONFIG=./test-config.json pinchtab bridge --engine chrome
pinchtab --server http://localhost:9871 health          # -> ok
```

## 3. Crawl → the graph finds the affordances

```bash
python3 -m pinchtab_webgraph.interaction_crawl \
  --start http://localhost:8099/index.html --server http://localhost:9871 \
  --out site-graph
```
```
· [2 states] depth 1 · …/reports.html (5 controls)
    ✓ download 'Download Q3 report' (direct)
    ✓ download 'Export CSV'
· [3 states] depth 1 · …/team.html (4 controls)
    ✓ trigger 'Create member' + form
Wrote site-graph.json: 4 states, 6 edges, 4 triggers
```

The triggers carry their terminal-action descriptor:

| label | kind | href | selector |
|---|---|---|---|
| Download Q3 report | `download` | `…/q3-report.pdf` | `#dl-q3` |
| Export CSV | `download` | — (JS) | `#export-csv` |
| Create member | `form` | — | `#save-member` |

## 4. How-to → path + runnable command block

```bash
python3 -m pinchtab_webgraph.howto site-graph.json \
  --goal "download q3 report" --start http://localhost:8099/index.html
```
```
Shortest route — 2 clicks:
  1. Go to http://localhost:8099/index.html
  2. Click "Reports"
  3. Download via "Download Q3 report"
This downloads a file: http://localhost:8099/files/q3-report.pdf

Run it with PinchTab:
  pinchtab nav 'http://localhost:8099/index.html'
  pinchtab nav 'http://localhost:8099/reports.html'   # Reports
  # --- download ---
  pinchtab download 'http://localhost:8099/files/q3-report.pdf' -o 'q3-report.pdf'
```

## 5. Perform → the browser really does it

**JS-triggered download** — navigates, clicks the export button, and the browser writes
the CSV to the profile's download directory:

```bash
python3 -m pinchtab_webgraph.perform --graph site-graph.json \
  --goal "export csv" --start http://localhost:8099/index.html \
  --server http://localhost:9871 --config ./test-config.json
```
```
=== PERFORM: EXPORT CSV ===  (download, ran)
  ✓ pinchtab nav 'http://localhost:8099/index.html'
  ✓ pinchtab nav 'http://localhost:8099/reports.html'   # Reports
  ✓ pinchtab click --css '#export-csv'   # JS-triggered — the session captures the file
```
→ `~/Downloads/data.csv` appears on disk with the exact bytes the button generates
(`a,b,c⏎1,2,3`). **Observed and verified.**

**Form fill + submit** — with real values and the submit un-gated:

```bash
python3 -m pinchtab_webgraph.perform --graph site-graph.json \
  --goal "create member" --start http://localhost:8099/index.html \
  --server http://localhost:9871 --config ./test-config.json \
  --set "Name=Ada Lovelace" --set "Role=Admin" --allow-submit
```
```
=== PERFORM: CREATE MEMBER ===  (form, ran)
  ✓ pinchtab nav … --nav … --click '#save-member'  (open form)
  ✓ pinchtab fill '#mname' 'Ada Lovelace'   # Name (required)
  ✓ pinchtab select '#mrole' 'Admin'   # Role
  ✓ pinchtab click --css '#save-member'   # submit: Create member
```
Reading the live DOM back afterwards: `{"name":"Ada Lovelace","role":"Admin"}`. **Verified.**

Without `--set`/`--allow-submit`, those field steps are **skipped** and the submit is
**not run** — safe by default.

## Caveats this test established

- **Direct `pinchtab download <url>` is SSRF-guarded**: it refuses `internal or blocked
  host` URLs, so it will not fetch a `localhost` file (returns HTTP 400). It works for
  normal external hosts. The **JS-triggered** download (a `click`) is not affected — use
  it to exercise downloads locally.
- The bridge must have `security.allowDownload = true` for either kind to save a file.

## Bugs this live test caught (now fixed + regression-tested)

1. **`interaction_crawl` dropped the terminal-action fields.** Its output serializer
   only copied `label/state/path/form/opensAt`, so every crawled `download` trigger lost
   its `kind`/`href`/`selector` and resolved as a zero-field, low-confidence match — so
   "download X" how-tos silently failed. Fixed by `serialize_trigger()` (unit-tested).
2. **`perform` sent `--tab` to commands that reject it.** `pinchtab download` has no
   `--tab` flag and errored. Fixed by targeting the tab via the `PINCHTAB_TAB` env var
   (which tab-aware commands read and others ignore), plus a `resolve_tab()` step so the
   run doesn't 404 on PinchTab's stale default tab.
