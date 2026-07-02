# Safe login for authenticated apps

`pinchtab-webgraph` can crawl sites that sit behind a login. This document explains
**exactly** what is implemented, where credentials live, the security properties it
guarantees, its limits, and how to test it yourself.

The whole feature is **opt-in**: nothing here runs unless you pass `--login-config`
to a crawl (or invoke `pinchtab_webgraph/login.py` directly). Default behaviour is
unchanged, and the base install stays pure-stdlib — `keyring` is an optional extra.

## Two ways in (safest first)

### 1. Log in by hand once — recommended, zero config
Open the persistent bridge profile, sign in, and crawl. The session cookie lives in
`.instance/` (gitignored) and the crawler reuses it. **Your password never touches
this toolkit.** This is the safest option and needs nothing below.

### 2. Automated login — opt-in, for unattended / long crawls
When the bridge may restart mid-crawl or you run on a schedule, enable keyring-backed
login so re-authentication happens without a human present:

```bash
pip install 'pinchtab-webgraph[login]'          # optional dependency (keyring)
cp login-config.example.json login-config.json  # gitignored — ROUTING only, no password
keyring set pinchtab-webgraph you@example.com    # the password goes into the OS keyring
interaction_crawl --start https://app.example.com/home --login-config login-config.json
```

## Where secrets live — the split

Credentials are deliberately split so **nothing secret is ever written to disk by this
toolkit**:

| Thing | Where it lives | In git? |
| --- | --- | --- |
| Login URL, username, optional field selectors | `login-config.json` (routing only) | **gitignored** |
| **The password** | **OS keyring** (`keyring set <service> <username>`) | never on disk here |
| Session cookie (hand-login path) | browser profile under `.instance/` | **gitignored** |
| Bridge auth token | `crawl-config.json` | **gitignored** |

The password is read from the keyring **at runtime only**, passed straight to the
browser's `fill` command, and **masked everywhere else** — logs show its length
(`pw=<20 chars>`), never the value. It never reaches the graph JSON, stdout, or any
config file.

## What was implemented

- **`pinchtab_webgraph/login.py`** (new module):
  - `_get_password(entry)` — resolves the password from the **OS keyring only**
    (`keyring.get_password(service, username)`). `import keyring` is **lazy**, so the
    base install needs no extra dependency; if it's missing (or the secret isn't
    stored) you get a clear one-line guiding message, not a traceback.
  - `DETECT_JS` — **structural** login-form field detection using standard HTML
    semantics only (no app-specific labels/vocabulary, per the project's generic rule):
    - password field: the visible `input[type=password]`;
    - username field: `input[autocomplete=username]` → `input[type=email]` → the
      visible text/tel input immediately **preceding** the password field in DOM order;
    - submit: `button[type=submit]` / `input[type=submit]`, else press **Enter**.
    Any of these can be overridden per host in the config.
  - `perform_login(entry, server)` — cold-start-safe navigation (adopts a live tab or
    forces a new one and pins it), fills username + password, submits, then `verify()`s.
  - `verify(entry, server)` — success = reached `successUrl` (if set), else left the
    login page **and** no password field is visible any more.
  - `ensure_logged_in(config_path, url, server)` — the crawler entry point: logs in if
    the config has an entry for the start URL's host; a no-op otherwise.
  - A CLI (`python -m pinchtab_webgraph.login …`) that is **reused as the
    wedge-recovery `--login-cmd`** so a restarted bridge re-authenticates the same way.
- **`interaction_crawl.py`** — new **`--login-config`** flag (default off). When set, it
  logs in **before** crawling and auto-wires re-auth on bridge restart. Everything is
  gated on the flag, so default behaviour is untouched.
- **`pyproject.toml`** — `optional-dependencies.login = ["keyring>=24"]`; base
  `dependencies` stay `[]`.
- **`.gitignore`** — `login-config.json`; a committed `login-config.example.json` carries
  no secret.

## Config reference — `login-config.json`

Per-host routing. Fields marked optional are auto-detected; set them only if detection
picks the wrong element.

```json
{
  "app.example.com": {
    "url": "https://app.example.com/login",
    "username": "you@example.com",
    "userField": null,
    "passField": null,
    "submit": null,
    "successUrl": "/dashboard",
    "keyringService": "pinchtab-webgraph"
  }
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `url` | yes | the login page to open |
| `username` | yes* | the username to fill and the keyring lookup key (*omit only if the form has no username field) |
| `userField` / `passField` / `submit` | no | CSS selector overrides when auto-detection is wrong |
| `successUrl` | no | substring that proves login worked (recommended) |
| `keyringService` | no | keyring service name (default `pinchtab-webgraph`) |

## Security properties

- The password is never in the config file, the graph JSON, logs, stdout, or the CLI
  argv — only in the OS keyring, read at runtime, masked in all output.
- The feature is off unless `--login-config` is passed.
- Field detection is structural, so no app names/routes leak into code (stays generic).

## Threat model — read this before trusting keyring

Be precise about what the OS keyring protects. It defends the **at-rest / passive**
boundary, and only that:

| Exposure route | Protected by keyring? |
| --- | --- |
| Committed to git | ✅ not in the repo (routing file is gitignored, holds no secret) |
| Leaked in logs / graph JSON / stdout | ✅ masked, never written |
| Visible in `ps` / shell history | ✅ never on the CLI |
| Stolen disk while logged-out / locked | ✅ encrypted at rest |
| **A process running as your user, session unlocked** | ❌ **readable — one command** |
| **A process with `sudo` / root** | ❌ **readable — root reads anything** |

The last two rows are the important ones. Once your desktop session has unlocked the
Secret Service, **any process running as you can read the secret** with a single call:

```bash
secret-tool lookup service pinchtab-webgraph username you@example.com
python -c "import keyring; print(keyring.get_password('pinchtab-webgraph','you@example.com'))"
```

That includes an AI agent with a shell. And if that process can `sudo` (passwordless
sudo is common on dev machines), then **no local secret store and no same-box OS-user
isolation helps** — root can `sudo -u <otheruser> secret-tool …` or read any file.
**Same-UID (or root) = same access.** Hiding *where* the secret lives does not help; the
keyring is enumerable and files are greppable. Obscurity is not a boundary.

So: **keyring is good at-rest hygiene, not a wall between you and a process that runs as
you.** Do not treat it as agent-proof.

## Keeping the credential genuinely out of an agent's reach

Only defenses that change *who can read it* or *how much a read is worth* hold against a
privileged local agent:

1. **Bot account (always do this).** A dedicated, least-privilege, rotatable account in
   the target app — never your personal login. Even under full local compromise the
   blast radius is bounded and you can revoke it independently of your own sessions.
2. **Move the boundary off the box the agent controls.** Run the bridge + login on a
   **separate host/VM** (or a container on a host you control). The agent drives it over
   the network with the bridge token but cannot `sudo` into a machine it isn't on, so the
   credential is unreachable. Strongest guarantee.
3. **De-privilege the agent.** Run the crawling agent as an **unprivileged user (no
   sudo)** and the bridge/login as a separate local user with a `chmod 700` home. Then the
   OS-user boundary genuinely holds on one box — but only because the agent lost root.
4. **Per-access interactive approval.** A password manager (1Password/Bitwarden) set to
   require biometric/master-password approval on *each* read stops silent pulls (it does
   not stop root reading an already-cached secret).

The agent-side crawler already talks to the bridge purely over HTTP + a token, so it works
against a remote/isolated bridge **without any code change** — point `--server` at it and
**do not** pass `--login-config` on the agent side (the isolated user performs the login;
the agent only ever drives the resulting session).

### Bot-account checklist

- Create a dedicated account in the target app; do **not** reuse your personal credentials.
- Grant the **minimum** permissions the crawl needs (ideally a read-only / viewer role).
- Store **its** password in the keyring (or on the isolated host) — never your own.
- Rotate on a schedule, and immediately if you suspect exposure; revoke its sessions
  independently of yours.

### Verifying an isolation boundary actually holds

From the agent's user, try to read the isolated store — expect `Permission denied`:

```bash
sudo -u "$AGENT_USER" cat /home/pwgcrawl/.local/share/keyrings/* 2>&1   # want: Permission denied
```

⚠️ If the agent's user has **passwordless sudo**, `sudo -u pwgcrawl secret-tool lookup …`
still succeeds — the boundary only holds when the agent lacks root. Check with
`sudo -n true` (if it returns without a prompt, local isolation will not stop that agent;
use options 1–2 above).

## Limits (not yet handled)

- **SSO redirects, 2FA, and CAPTCHA** are not automated. The flow assumes a single-page
  username + password form. For multi-step identity providers, use the **hand-login**
  path (option 1) instead.
- `--login-config` is wired into `interaction_crawl` only (not yet the `recipe.py` /
  `ask.py` live paths).

## How to test it yourself

This is exactly how the feature was verified against a public test-login site
(`the-internet.herokuapp.com`, credentials `tomsmith` / `SuperSecretPassword!`).

```bash
# 0. optional dependency + a throwaway venv if your system pip is externally managed
python3 -m venv /tmp/loginvenv && /tmp/loginvenv/bin/pip install keyring

# 1. start an isolated bridge (in a real terminal — the harness can't bind sockets)
#    a minimal crawl-config.json with allowedDomains incl. the test host, IDPI off.
./start-crawl-browser.sh          # or your own bridge on some port, e.g. 9873

# 2. store the test password in the OS keyring
/tmp/loginvenv/bin/python -c "import keyring; keyring.set_password('pinchtab-webgraph','tomsmith','SuperSecretPassword!')"

# 3. routing file (NO password)
cat > login-config.json <<'JSON'
{ "the-internet.herokuapp.com": {
    "url": "https://the-internet.herokuapp.com/login",
    "username": "tomsmith", "successUrl": "/secure" } }
JSON

# 4a. run the login flow directly — expect "login OK" and exit 0
/tmp/loginvenv/bin/python -m pinchtab_webgraph.login \
  --config login-config.json --server http://localhost:9873 \
  --pinchtab-config crawl-config.json --host the-internet.herokuapp.com

# 4b. or the full crawler — logs in, THEN crawls the authenticated page
/tmp/loginvenv/bin/python -m pinchtab_webgraph.interaction_crawl \
  --start https://the-internet.herokuapp.com/secure \
  --server http://localhost:9873 --login-config login-config.json \
  --max-states 3 --max-depth 1 --no-read-forms --no-capture-content --out /tmp/lt-graph

# 5. negative check: `keyring set ... tomsmith WRONGpass` → login exits 1 ("could NOT be confirmed")
# 6. clean up: keyring delete_password('pinchtab-webgraph','tomsmith'); rm login-config.json; stop the bridge
```

**Verified results:** cold-start login → `login OK`, exit 0; wrong password → `could NOT
be confirmed`, exit 1 (pw masked as `<13 chars>`); `interaction_crawl --login-config`
logs in then crawls `/secure`. Testing also caught and fixed a cold-start tab-pinning
bug (a fresh bridge has no tab + a stale default id, so a plain `nav()` left evals
hitting no tab — `perform_login` now adopts or force-creates and pins a tab first).
