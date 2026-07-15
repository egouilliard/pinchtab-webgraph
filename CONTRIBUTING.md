# Contributing to pinchtab-webgraph

Thanks for your interest in contributing! This is a small, deterministic toolkit that maps any website into a navigation + content graph through the [PinchTab](https://github.com/egouilliard) browser-automation CLI. Contributions — code, docs, bug reports, or test sites — are all welcome, and every contributor should feel supported.

Please read the project [`README.md`](README.md) first for setup, the tool inventory, and how to run a crawl. This guide covers how to propose changes.

## Quick Links

- [README](README.md) · [Documentation index](docs/README.md)
- [All issues](https://github.com/egouilliard/pinchtab-webgraph/issues)
- [Discussions](https://github.com/egouilliard/pinchtab-webgraph/discussions)
- [License (MIT)](LICENSE)
- [Code owners](CODEOWNERS)

## Repository Layout

Keep the root clean. Everything has a home:

| Path | What lives there |
| --- | --- |
| `pinchtab_webgraph/` | The Python package — all crawler / path-finder / API / CLI / MCP / UI code. |
| `pinchtab_webgraph/ui/static/` · `vendor/` | Shipped web-UI assets and vendored JS (tracked; the wheel bundles them). |
| `examples/flows/` | Committed, runnable example [flow documents](docs/flows.md) (e.g. `download-all-reports.json`). |
| `scripts/` | Runnable shell wrappers — `start-crawl-browser.sh`, `run-*.sh`, `bench.sh`, `hard-bench.sh`, etc. Invoke them as `scripts/<name>.sh`. |
| `tests/` | Unit + `tests/e2e/` (needs a live bridge; HTML fixtures in `tests/e2e/*.html` are tracked). |
| `docs/` | Long-form docs, indexed by [`docs/README.md`](docs/README.md). |
| `out/` | **All generated artifacts** — graphs, how-tos, screenshots, benchmark JSON/PNG, bridge logs. **Gitignored; never committed.** |
| `caches/` | Per-host interaction-graph caches (`ask.py` write-back). Gitignored runtime data. |
| root | Only project metadata + example configs: `README.md`, `CONTRIBUTING.md`, `LICENSE`, `CODEOWNERS`, `pyproject.toml`, `MANIFEST.in`, `.gitignore`, `*.example.json`. |

**Generated output goes in `out/`.** The tools default `--out` to `out/…` (e.g. `out/webgraph.json`, `out/recipe.png`) and create parent dirs automatically — so a normal run never litters the root. If you add a tool or script that writes files, default its output under `out/` too, and never `git add` anything from it. The loose-in-root ignore patterns in `.gitignore` are only a backstop.

## 1. Ways to Contribute

You don't need to write code to help:

- Reporting bugs and edge cases (especially sites where the crawler mis-behaves)
- Suggesting features and improvements
- Improving documentation, examples, and the README
- Reviewing open pull requests
- Adding new test-target sites or benchmark scenarios
- Writing or improving tests

## 2. Getting Started

1. **Read** the [`README.md`](README.md) for install, prerequisites (a running PinchTab bridge), and how to run `interaction_crawl.py` / `howto.py` / `recipe.py` / `crawl.py`.
2. **Fork** the repository and **clone** your fork locally.
3. **Create a branch** from `dev` (see the branch model below).
4. **Make your changes** and test them locally against at least one real site.
5. **Commit** with a conventional, signed-off message (sections 4 and 5).
6. **Push** to your fork and open a **Pull Request** against `dev`.

## 3. Branch Model

The repository follows a protected branch model:

| Branch | Purpose | Protected |
| --- | --- | --- |
| `main` | Production-ready code. All changes land here via PR. | Yes |
| `release` | Release-candidate stabilisation before tagging a version. | Yes |
| `hotfix` | Urgent fixes that need to skip the normal cycle. | Yes |
| `dev` | Day-to-day integration branch where work lands first. | No |

Cut feature branches from `dev`. Use descriptive prefixes:

- `feat/<short-description>` — new features
- `fix/<short-description>` — bug fixes
- `docs/<short-description>` — documentation
- `chore/<short-description>` — tooling, CI, refactors
- `test/<short-description>` — tests only

All protected branches (`main`, `release`, `hotfix`) require at least one **Code Owner** approval before merging — see [`CODEOWNERS`](CODEOWNERS). Force-pushes and deletions are disabled on those branches, and stale reviews are dismissed when new commits are pushed.

## 4. Commit Messages

We use [Conventional Commits](https://www.conventionalcommits.org/). This keeps history readable.

```
feat(crawl): capture repeated-sibling clusters as content collections
fix(recipe): re-pin the active tab after navigation
docs(readme): document --cross-host iframe traversal
chore(ci): bump action versions
test(howto): cover offline --find over virtualized grids
```

Scope (in parentheses) is optional but encouraged — use the module or area most affected (`crawl`, `howto`, `recipe`, `ask`, `paths`, `viewer`).

## 5. Sign Off Your Commits

Sign off each commit to certify you have the right to submit it under the project's license (the [Developer Certificate of Origin](https://developercertificate.org/)). Use the `-s` flag:

```shell
git commit -s -m "feat(crawl): add virtualization scroll-loading"
```

Handy alias:

```shell
git config alias.cos "commit -s"
# then: git cos -m "..."
```

The sign-off appends a `Signed-off-by: Your Name <your@email>` trailer.

## 6. The One Hard Rule: Stay Generic

**This is non-negotiable and the most common reason a PR is sent back.** The crawler and path-finder must work on *any* website. Do **not** hardcode app-specific routes, labels, selectors, or vocabulary in the discovery/logic code (`interaction_crawl.py`, `recipe.py`, `howto.py`, `crawl.py`, `paths.py`, `api.py`, `query_cmd.py`, `mcp_server.py`, `utcp_manual.py`). Use structural signals only:

- ARIA roles and states (`role=grid/table/tree/list/feed`, `aria-selected/pressed/current`, …)
- Repeated-sibling / cluster detection
- URL-path grouping

App-specific strings belong **only** in config and benchmark files (e.g. `crawl-config.json`, `scripts/hard-bench.sh`). Individual apps are test *targets*, never something the code special-cases. A PR that adds an app name or a hand-picked selector into the logic will not be merged.

## 7. Stay Safe

Discovery must never mutate data. It opens and reads forms, then presses Escape — create / save / delete / submit controls are recorded, not clicked, unless a single target trigger is being opened to read its form. Never add code that submits or persists during exploration, and never run a "click everything" crawl against an authenticated session you care about.

## 8. Pull Requests

When opening a PR:

- **Keep it focused** — one logical change per PR.
- **Target `dev`.**
- **Say what, why, and how you tested it** — include the site(s) you crawled and before/after numbers if it affects coverage or performance.
- **Include tests** where practical; new behaviour should be reproducible.
- **Verify generality** — re-grep your diff for app-specific strings before submitting (section 6).
- **Keep secrets out** — never commit `crawl-config.json`, `.instance/`, caches, or crawl artifacts. Commit explicit source files only; never `git add -A`.
- **Match the existing style** and resolve every review comment before requesting re-review.

PRs are reviewed by the code owner. Expect first feedback within a few days.

## 9. Reporting Issues

Use the [**Issues**](https://github.com/egouilliard/pinchtab-webgraph/issues) tab. Please include:

- A clear title and description
- The site / URL pattern involved (or a public site that reproduces it)
- The exact command you ran and the relevant log output
- Expected vs actual behaviour
- Environment details (OS, Python version, PinchTab version)

Search existing issues first to avoid duplicates.

## 10. Reporting Security Issues

**Please do not open a public issue for a security vulnerability.** Report it privately by opening a [GitHub security advisory](https://github.com/egouilliard/pinchtab-webgraph/security/advisories/new) or contacting the maintainer directly, and allow time for a fix before public disclosure.

## 11. Code of Conduct

Be respectful, constructive, and patient in all project spaces — issues, PRs, and discussions. Harassment or exclusionary behaviour won't be tolerated. Everyone was new once.

## 12. License

By contributing, you agree that your contributions will be licensed under the project's [MIT License](LICENSE).

---

Thanks for helping make pinchtab-webgraph better.
