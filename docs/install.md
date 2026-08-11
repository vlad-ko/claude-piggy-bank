# Installing CPB

The short version is in [`README.md`](../README.md#install). This file is the
rest of it: the two ways to run CPB, what the hooks do, where the database
lives, and how to upgrade and uninstall without losing history.

- [Two ways to run the same code](#two-ways-to-run-the-same-code)
- [As a plugin, from the marketplace](#as-a-plugin-from-the-marketplace)
- [As a plugin, from a clone you read first](#as-a-plugin-from-a-clone-you-read-first)
- [As a plain checkout](#as-a-plain-checkout)
- [What the hooks do](#what-the-hooks-do)
- [Where the plugin keeps your database](#where-the-plugin-keeps-your-database)
- [Upgrade](#upgrade)
- [Uninstall](#uninstall)

## Two ways to run the same code

CPB is one repository that can be used two ways. **The plugin and the checkout
are the same code** — the hooks run the same `ingest.py` you would run by hand,
nothing is compiled, and there are no dependencies either way.

| | **as a Claude Code plugin** | **as a plain checkout** |
|---|---|---|
| suits | using CPB on your own ongoing work | reading the code before trusting it with your history; analysing one project on demand; developing on CPB |
| install | `/plugin install` from CPB's own marketplace, or a clone into your skills directory | `git clone` |
| ingest | automatic — three hooks ingest each transcript as it is written | manual — you run `cpb.py ingest` when you want it |
| database | `${CLAUDE_PLUGIN_DATA}/usage.db`, one per install, survives updates | `db/usage.db` beside the checkout |
| report | `/claude-piggy-bank:cpb` from inside a session | `python3 cpb.py serve` |

**Prefer the plugin if you intend to keep using CPB.** Subagent transcripts are
reaped, and its `SubagentStop` hook is the difference between capturing that
spend and losing it — see [What the hooks do](#what-the-hooks-do). Prefer the
checkout if you want to look first; you can enable the plugin later over the
same clone.

## As a plugin, from the marketplace

CPB is its own marketplace: the repository is both the catalog and the plugin.
From inside Claude Code:

```
/plugin marketplace add vlad-ko/claude-piggy-bank
/plugin install claude-piggy-bank@claude-piggy-bank
```

or, without starting a session:

```bash
claude plugin marketplace add vlad-ko/claude-piggy-bank
claude plugin install claude-piggy-bank@claude-piggy-bank
```

**Both lines are needed.** `marketplace add` registers the catalog and installs
nothing — it only lets you browse what is in it. The second line is the one that
puts CPB on your machine, and skipping it leaves you with a marketplace and no
plugin, which looks like a successful install until the first time you reach for
the report.

The name is doubled because the marketplace and the plugin are the same
repository — `<plugin>@<marketplace>` is the form Claude Code installs by. If
the install summary says `Run /reload-plugins to activate.`, run it.

## As a plugin, from a clone you read first

The marketplace install copies the code into a cache. If you would rather read
it before it runs on your machine — which, for a tool that reads your
transcripts, is a reasonable thing to want — clone into your personal skills
directory instead. Claude Code loads any folder there that contains a
`.claude-plugin/plugin.json` as a plugin on the next session, in place, with no
install step and no copy into a cache. The reference calls these
*skills-directory plugins*, and they are as documented as the marketplace path:

```bash
git clone https://github.com/vlad-ko/claude-piggy-bank.git \
  ~/.claude/skills/claude-piggy-bank
```

Restart Claude Code and it loads as `claude-piggy-bank@skills-dir`. Update it
with `git pull`; there is nothing to uninstall.

**The command is the same either way.** A skills-directory folder holding a
`.claude-plugin/plugin.json` is a *plugin*, not a loose skill, so the skill it
bundles is namespaced by the plugin exactly as it is after a marketplace
install: `/claude-piggy-bank:cpb`. Clone into a folder of that name, as above,
and the two paths agree.
(Documented: [Plugins reference § Skills-directory
plugins](https://code.claude.com/docs/en/plugins-reference#skills-directory-plugins)
and [Skills § How a skill gets its command
name](https://code.claude.com/docs/en/skills#how-a-skill-gets-its-command-name),
both checked 2026-08-07. Not separately measured on a skills-directory install
here — the measurement in [Where the plugin keeps your
database](#where-the-plugin-keeps-your-database) is of the marketplace path.)

To load it for one session without installing it anywhere:

```bash
git clone https://github.com/vlad-ko/claude-piggy-bank.git
claude --plugin-dir ./claude-piggy-bank
```

## As a plain checkout

```bash
git clone https://github.com/vlad-ko/claude-piggy-bank.git
cd claude-piggy-bank
python3 cpb.py ingest
python3 cpb.py serve      # then open http://127.0.0.1:8377/
```

Nothing is installed onto your system and nothing is added to your `PATH`:
`cpb.py` is a file in the checkout that you run with `python3`, which is why
every command in these documents names it that way. The flags are in
[`cli.md`](cli.md).

## What the hooks do

Enabling the plugin activates three triggers. **No edit to any
`.claude/settings.json` is needed.**

| trigger | when it fires | what it ingests |
|---|---|---|
| `SubagentStop` | a subagent finishes | that subagent's own transcript |
| `Stop` | Claude finishes a response | the session transcript, per turn |
| `SessionEnd` | the session ends | the session transcript, best-effort |

Each one spawns `ingest.py --transcript` for **exactly one file** — no directory
scans, no network, no model. `SubagentStop` is the one that earns its place:
subagent transcripts are reaped, and on the reference corpus 211 subagent runs
are already permanently unmeasurable while subagents are ~78% of all API calls.
Ingesting the moment a subagent finishes is the difference between capturing
that spend and losing it.

If an ingest fails, the hook says so in one line in your transcript and appends
it to `cpb-hook.log` beside the database — and then **gets out of the way**. It
never blocks a turn, never stops a subagent, and never reports success for work
it did not do. The reasoning, and the plugin design generally, is in
[`plugin.md`](plugin.md).

You do not have to wait for a hook to see current numbers:
`/claude-piggy-bank:cpb` refreshes before it opens the report, so the turn you
are in is measured too. A refresh re-reads only files whose size or mtime
changed — 0.22 s over this project's own 75 transcripts, measured 2026-08-11 on
macOS — and if it fails the report still opens, over the data already in the
database, saying how old that data is.

### If the hooks do not run at all

Claude Code launches each hook by resolving the name `python3` on your `PATH`,
with no shell involved — the documented form for a bundled script, and the only
one available. **On a machine where Python is not called `python3` there,
nothing ingests, and no error is shown.** Windows is where this bites: the
interpreter is `python.exe`, the launcher is `py.exe`, and the Microsoft Store
ships a `python3.exe` that opens the Store rather than running anything. (This
is established from Claude Code's [hooks
reference](https://code.claude.com/docs/en/hooks#exec-form-and-shell-form),
checked 2026-08-11, and the Store stub's documented behaviour — **not from an
observed Windows run**; CPB has no Windows machine to verify it on. If you hit
it, or do not, please say so on
[#116](https://github.com/vlad-ko/claude-piggy-bank/issues/116).)

The report will tell you. A successful hook run leaves a record beside the
database, so once enough turns have completed since the install with no record
written, the page says **"AUTOMATIC INGEST IS NOT RUNNING"** and stops there
rather than leaving you with an old number and no explanation. To check by
hand:

- `/hooks` lists every configured hook — CPB's three appear under *Plugin
  Hooks* if the plugin is enabled at all;
- `python3 --version` in the same shell Claude Code runs in says whether that
  name resolves;
- `claude --debug` writes each hook's exit code and stderr to
  `~/.claude/debug/<session-id>.txt`.

There is no supported way to point the hooks at a differently-named interpreter
today; `/claude-piggy-bank:cpb` still refreshes and serves, so the report stays
current whenever you open it. Why no portable launcher exists, and what would
be needed to ship one, is in [`plugin.md`](plugin.md#which-interpreter-launches-the-hooks--and-why-it-is-not-portable).

## Where the plugin keeps your database

`${CLAUDE_PLUGIN_DATA}/usage.db` — a directory that survives plugin updates,
which is the whole reason it is there rather than beside the plugin's code.
Set `CPB_DB` yourself to override it; the hooks will not touch a value you have
chosen.

That is not only the documented behaviour; it was measured. Installing from the
marketplace, running a session, bumping the version, publishing and updating
(2026-08-07, Claude Code 2.1.223; the two versions below are that throwaway
install's, not a release plan): the plugin directory moved from
`…/plugins/cache/claude-piggy-bank/claude-piggy-bank/1.6.0` to `…/1.7.0`, the
data directory did not move at all, and `usage.db` came through the update
byte-identical. The session recorded before the update was still in it
afterwards, next to the one recorded after.

The skill opens that database for you. If you run either script by hand instead,
pass it explicitly — `serve.py` reads only `--db`, not `CPB_DB`, and while
`ingest.py` does read `CPB_DB`, nothing sets it in a session you started
yourself. Without the flag each falls back to its own default beside the code,
so you would fill one database and read another:

```bash
python3 cpb.py serve  --db ~/.claude/plugins/data/<plugin-id>/usage.db
python3 cpb.py ingest --db ~/.claude/plugins/data/<plugin-id>/usage.db
```

The full resolution order, and what happens inside an install where "beside the
script" is a directory the next update replaces, is in
[`cli.md`](cli.md#where-the-database-lives).

## Upgrade

Installed from the marketplace:

```
/plugin update claude-piggy-bank@claude-piggy-bank
```

Cloned into your skills directory:

```bash
git -C ~/.claude/skills/claude-piggy-bank pull
```

Then `/reload-plugins`, or restart — hook changes are not picked up mid-session.
Your database is not in the plugin directory, so an upgrade cannot disturb it.

**An update only reaches you when CPB's version number moves.** Claude Code
keys its plugin cache on the `version` in the plugin manifest, so a fix
published without a bump would leave `/plugin update` reporting you were
already current. CI fails any change to the shipped plugin that leaves that
field alone; the mechanism is in [`versioning.md`](versioning.md).

## Uninstall

Installed from the marketplace:

```bash
claude plugin uninstall claude-piggy-bank@claude-piggy-bank --keep-data
```

**Pass `--keep-data`, or back the database up first.** Without it,
uninstalling from the last scope deletes the plugin's data directory — and past
Claude Code's ~30-day transcript retention that database is the only copy of
your history. See [Transcripts expire — back up the
database](data-sources.md#transcripts-expire--back-up-the-database).

Cloned into your skills directory, delete the folder; nothing was installed, so
there is no uninstall step, and your database is not in the folder you are
deleting. To stop loading it without deleting:

```bash
claude plugin disable claude-piggy-bank@skills-dir
```
