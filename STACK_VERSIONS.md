# Coded Vision Design — canonical stack versions

Source of truth for the versions every new scaffold should use. Older
versions are only acceptable when an existing repo's lockfile demands
them.

**Last checked: 2026-09-11** (manual refresh against npm dist-tags and
the official PHP, PostgreSQL, MariaDB, Redis and Node.js release feeds).
The weekly `refresh-stack-versions.py` cron described under *Refresh
process* has not been written yet, so this table is refreshed by hand
for now. Once the cron opens PRs, review the diff before merging: a
regression in a tool's latest stable can ship via this table.

## Frontend

| Package | Version | Notes |
| --- | --- | --- |
| Next.js | 16.3.4 | App Router default. Server Components + `use()` + `useActionState` for new pages. 16.3 is the first line that accepts the TypeScript 7 compiler; 16.2 and earlier reject it at build time unless `experimental.useTypeScriptCli` is set. |
| React | 19.3.0 | Server Components on by default. No class components in new code. |
| TypeScript | 7.0.2 | Strict mode. No `any`. Infer where possible. 7.x is the native (Go) compiler; the `tsc` CLI and tsconfig options used across CVD repos are unchanged. Needs Next.js 16.3 or later (see above). |
| Tailwind CSS | 4.3.3 | CSS-first `@theme` config — no `tailwind.config.js` for new projects. |
| Vite | 8.3.0 | For pure-SPA / static projects. Rolldown-based. |
| Vitest | 5.0.0 | Companion to Vite. 5.0.0 is a new major (September 2026); check its migration notes before bumping an existing 4.x repo. |
| shadcn/ui | CLI 4.21.0 | Add components via `npx shadcn@latest add …` — no global install. |
| lucide-react | 1.44.0 | Icon set. Tree-shakes cleanly. |
| Playwright | latest (1.63.0 at last check) | E2E + the jarvis-browser PDF endpoint use the same image. |

## Runtime

| Package | Version | Notes |
| --- | --- | --- |
| Node.js | 24 (Active LTS, 24.21.0) | Node 26 (26.8.2) is Current and becomes LTS in October 2026; kept off the default until it has had a quarter of cooking as LTS. |
| npm | bundled with Node 24 | Lockfile must be committed. No yarn / pnpm unless project already uses it. |

## Backend / data

| Package | Version | Notes |
| --- | --- | --- |
| PHP | 8.4.x (8.4.25) | For the codedvisiondesign.co.uk admin + WordPress client sites. 8.5 (8.5.10) is also stable; 8.4 stays the default here. |
| MariaDB | 11.4.13 (LTS) | For the legacy MySQL workloads. 11.8 (11.8.9) is the newer LTS line. |
| Postgres | 16.x (16.15) | For postgres-shared on the VPS (jarvis, cdvdb). 17 and 18 are stable; 16 stays until that instance is upgraded. |
| Redis | 8.x (8.10.1) | Cache + queue layer. |

## Why this file

Before this file existed, three sources disagreed:

- `CODING_STANDARDS.md` (Jarvis agent prompts) — said Next 15 / Vite 6 /
  Tailwind 4 / Node 22, frozen at write time.
- `CLAUDE.md` (user global) — same.
- `bots/claudia/scaffolder.py` (template) — shipped React 18 / Tailwind 3
  / Vite 5, two major versions behind.
- Jarvis itself ran Next 16, contradicting all of the above.

Phase G2 collapsed these into one source. Other docs reference this
file rather than hardcoding versions:

- `prompts/CODING_STANDARDS.md` Frontend / Backend stack sections.
- `~/.claude/CLAUDE.md` MANDATORY WEBSITE RULES § Stack.
- `bots/claudia/scaffolder.py` reads this file at scaffold time (when
  the scaffolder rewrite lands).

## Refresh process

**Status (2026-09-11):** `scripts/refresh-stack-versions.py` does not
exist yet, so everything below is the intended design rather than a
running process. Until it lands, refresh by hand using the same
sources listed here.

Weekly cron at Monday 09:00 GMT runs
`scripts/refresh-stack-versions.py`. For each tracked package it
queries the official channel:

- npm packages → `npm view <pkg> version`
- GitHub-tracked → `gh release list --repo <upstream> --limit 1`
- OS packages (PHP / MariaDB / Redis) → official release feeds

Deltas are opened as a PR titled
`chore: bump stack versions (YYYY-MM-DD)` with the diff. Merge if the
upstream release notes look clean; close if a regression is reported.

Manual refresh: `python scripts/refresh-stack-versions.py`.

## Things to add when they happen

- Stripe SDK version when H6 wires the Stripe sync cog.
- Deepgram SDK version (currently bundled with the voice runtime).
- ElevenLabs SDK version (TTS).
