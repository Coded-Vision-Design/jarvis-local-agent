# Coded Vision Design — canonical stack versions

Source of truth for the versions every new scaffold should use. Older
versions are only acceptable when an existing repo's lockfile demands
them.

**Last checked: 2026-05-20** — refreshed weekly by the Monday 09:00 GMT
`refresh-stack-versions.py` cron (see `scripts/`), or on demand by
running it manually. If the cron opens a PR, review the diff before
merging — a regression in a tool's latest stable can ship via this
table.

## Frontend

| Package | Version | Notes |
| --- | --- | --- |
| Next.js | 16.2.6 (LTS) | App Router default. Server Components + `use()` + `useActionState` for new pages. |
| React | 19.2.6 | Server Components on by default. No class components in new code. |
| TypeScript | 6.0.3 | Strict mode. No `any`. Infer where possible. |
| Tailwind CSS | 4.3 | CSS-first `@theme` config — no `tailwind.config.js` for new projects. |
| Vite | 8.0.13 | For pure-SPA / static projects. Rolldown-based. |
| Vitest | 4.1.7 | Companion to Vite. |
| shadcn/ui | CLI v4 | Add components via `npx shadcn@latest add …` — no global install. |
| lucide-react | 1.16.0 | Icon set. Tree-shakes cleanly. |
| Playwright | latest | E2E + the jarvis-browser PDF endpoint use the same image. |

## Runtime

| Package | Version | Notes |
| --- | --- | --- |
| Node.js | 24 (Active LTS) | Node 26 is current but kept off the default until a quarter of cooking. |
| npm | bundled with Node 24 | Lockfile must be committed. No yarn / pnpm unless project already uses it. |

## Backend / data

| Package | Version | Notes |
| --- | --- | --- |
| PHP | 8.4.x | For the codedvisiondesign.co.uk admin + WordPress client sites. |
| MariaDB | 11.4.10 (LTS) | For the legacy MySQL workloads. |
| Postgres | 16.x | For postgres-shared on the VPS (jarvis, cdvdb). |
| Redis | 8.x | Cache + queue layer. |

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
