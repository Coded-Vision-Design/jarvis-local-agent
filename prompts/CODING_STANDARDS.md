# Coded Vision Design - Coding Standards

These standards apply to ALL code you write or modify. Never deviate without explicit instruction.

---

## MANDATORY WEBSITE RULES (non-negotiable for every website project)

These rules apply to every website, landing page, and web app. The build must fail if any are violated. No exceptions without explicit user override.

### 1. Fully responsive: mobile, tablet, desktop

- **Mobile-first** CSS. Design and build for 375 px wide first, then scale up.
- **Required breakpoints** (Tailwind defaults are acceptable):
  - `sm`: 640 px (large phone / small tablet portrait)
  - `md`: 768 px (tablet)
  - `lg`: 1024 px (small laptop)
  - `xl`: 1280 px (desktop)
  - `2xl`: 1536 px (large desktop)
- Test every page at: 375 px, 414 px, 768 px, 1024 px, 1280 px, 1536 px.
- Use `clamp()` for fluid typography between breakpoints.
- Touch targets minimum 44 px tall on mobile.

### 2. Sticky / fixed header at top

- Every page has a header pinned to the top of the viewport.
- Implementation: `position: sticky; top: 0; z-index: 50;` on the `<header>` element.
- Header must remain visible during scroll. It must NOT overlap content (use scroll-padding-top or top body padding equal to header height).
- Background must be opaque (or `backdrop-filter: blur()` for glassmorphism) so content scrolling beneath remains legible.
- Header height max 80 px on desktop, max 64 px on mobile.

### 3. No horizontal scrolling - ever

- The `<html>` and `<body>` must never scroll horizontally on any breakpoint.
- Add to base CSS:
  ```css
  html, body { overflow-x: hidden; max-width: 100vw; }
  ```
- Use `min-w-0` on flex children to prevent overflow.
- Test by setting body background to a vivid colour and scrolling - no gap on the right side.
- Images, videos, embeds, code blocks must all use `max-width: 100%`.

### 4. Performance optimisation (required for every build)

- Lighthouse Performance score must be **>= 90** on mobile.
- Largest Contentful Paint (LCP) **<= 2.5 s**.
- Cumulative Layout Shift (CLS) **<= 0.1**.
- Total Blocking Time (TBT) **<= 200 ms**.
- Images: serve WebP or AVIF; lazy-load below-the-fold images (`loading="lazy"`).
- Fonts: preload critical fonts; use `font-display: swap`.
- Code: split routes; tree-shake; minify; gzip/brotli compression in production.
- No render-blocking JavaScript in `<head>` - defer or async everything.

### 5. Accessibility (WCAG 2.2 AA)

- All images have meaningful `alt` text (empty `alt=""` for decorative only).
- Form inputs have associated `<label>` elements.
- Colour contrast >= 4.5:1 for body text, >= 3:1 for large text and UI elements.
- All interactive elements keyboard-accessible with visible focus ring.
- Skip link to main content at top of every page.
- Semantic HTML: `<main>`, `<nav>`, `<header>`, `<footer>`, `<article>`, never `<div>` for everything.

### 6. SEO essentials

- Every page has unique `<title>` (50-60 chars) and `<meta name="description">` (150-160 chars).
- Open Graph + Twitter Card meta tags for shareable previews.
- `robots.txt` and `sitemap.xml` at the site root.
- Canonical URLs on every page.
- Structured data (JSON-LD) for the homepage and any product/service pages.

### 7. Deployment expectations

- Every website project deploys automatically to `https://<repo-slug>.codedvisiondesign.co.uk/` after a successful task.
- The deploy URL is added to the repo's README.md and the PR description.
- The `dist/` directory is the source of truth for what gets deployed.

---

## Language and Typography

- **British English only** throughout all code, comments, strings, and documentation.
  - colour, centre, behaviour, licence, authorise, optimise, organise, recognise
- **No em-dashes** (-- or —) in any file. Use a spaced hyphen ` - ` or rewrite the sentence.
- **No hardcoded secrets** - all credentials, API keys, tokens, and URLs go in environment variables.
- Use clear, concise British English in comments and documentation.

---

## Frontend

**Canonical versions: [STACK_VERSIONS.md](../STACK_VERSIONS.md).** Always pull the latest stable from that table when scaffolding new projects. Older versions are only acceptable when an existing repo's lockfile demands them. The list below names the patterns to use; the table names the version numbers.

- **React**: use `use()`, `useActionState`, Server Components where applicable. No class components.
- **Tailwind**: CSS-first config (`@theme` in CSS, not `tailwind.config.js`). No v3 patterns.
- **TypeScript**: strict mode. No `any`. Infer types where possible.
- **Package manager**: npm. Lock file must be committed. No yarn / pnpm unless project already uses it.
- **Node**: LTS-current (see STACK_VERSIONS.md).
- **Bundler**: Vite (latest) for pure-SPA; Next.js (App Router, latest LTS) for full-stack.

---

## Backend / PHP

- **PHP**: see STACK_VERSIONS.md for the current version. Use named arguments, readonly properties, enums, fibers where appropriate.
- **No PHP 7 patterns**: no old-style constructors, no eregi, no mysql_* functions.
- **Database**: MariaDB (version per STACK_VERSIONS.md) - accessible at `jarvis-db:3306` inside Docker, `127.0.0.1:17923` from host.
- **ORM**: prefer Eloquent (Laravel) or PDO with prepared statements. Never raw unparameterised queries.
- **Composer**: always `composer.lock` committed.

---

## Database

- **MariaDB** (version per STACK_VERSIONS.md) for relational data.
- **Postgres** for postgres-shared on the VPS (`jarvis`, `cdvdb` schemas).
- **Redis** for sessions, caching, queues - at `jarvis-redis:6379` inside Docker, `127.0.0.1:17924` from host.
- Migrations over raw SQL dumps. Always reversible.

---

## Python

- **Python 3.12+**. Type hints everywhere. Pydantic for data models.
- Async (asyncio/httpx) for I/O. No blocking calls in async context.

---

## Testing Strategy - mandatory layers for every project

Run the right test type at the right time. Both bug fixes and new builds must pass every applicable layer before the task is considered done.

### Test layers (in execution order)

| # | Layer | When to run | Speed | What it checks |
|---|-------|-------------|-------|----------------|
| 1 | **Smoke tests** | After every code change | < 30 s | App boots, main routes return 200, critical paths don't crash |
| 2 | **Unit tests** | After every function change | < 2 min | Pure-function behaviour with mocked dependencies |
| 3 | **Non-mutating integration tests** | After every code change | < 3 min | Read-only DB/API calls, no state mutation |
| 4 | **API tests (mocked)** | After API contract changes | < 1 min | Endpoint behaviour using fixtures and mocked HTTP |
| 5 | **DB tests (transactional)** | After model/schema changes | < 5 min | DB queries inside a transaction that ROLLS BACK at the end |
| 6 | **Regression tests** | After refactors or large changes (> 5 files / > 100 lines) | < 10 min | Full happy-path suite covering existing features |
| 7 | **E2E / browser tests** | Before deploy | < 15 min | Real browser via Playwright against a built site |

### Smoke tests

- One-shot startup checks: app boots, no import errors, healthcheck endpoint returns 200.
- Run as the first command in CI and the self-healing loop.
- File location: `tests/smoke.{ts,php,py}`.

```typescript
// tests/smoke.test.ts
import { describe, it, expect } from 'vitest'
import { app } from '../src/app'

describe('smoke', () => {
  it('app imports without throwing', () => {
    expect(app).toBeDefined()
  })
  it('GET / returns 200', async () => {
    const res = await app.request('/')
    expect(res.status).toBe(200)
  })
})
```

### Non-mutating tests

- Tests that hit real services or the DB but never write/delete/update.
- Must be safe to run against staging - no risk to data.
- Tag them `@non-mutating` or place under `tests/read-only/`.

### API tests without hitting rate limits

- **Never call external APIs in tests.** Use one of these patterns:
  - **MSW** (Mock Service Worker) for HTTP mocking in JS/TS.
  - **VCR.py** or **responses** library for Python.
  - **HTTP fixtures** committed to `tests/fixtures/` for replay.
- Provide a **fake API key** in `.env.test` so tests can never accidentally hit prod.
- For tests that absolutely must hit a real third-party API (e.g. contract tests), put them under `tests/contract/` and skip by default. Run them on a schedule, not on every commit.

### DB tests without destroying data

- **Use transactions that rollback.** Wrap every test in `BEGIN ... ROLLBACK` so the DB is unchanged after.
- **Use a dedicated test DB** named `<app>_test` - never run tests against production or development DBs.
- **Snapshot before / restore after** for tests that genuinely need state mutations (e.g. migrations).
- For PHP: use Laravel's `DatabaseTransactions` trait or PHPUnit's `@dataProvider` with rollback.
- For Python: use pytest fixtures with `pytest-postgresql` or SQLAlchemy's nested transactions.
- For TypeScript: use Drizzle's `db.transaction()` or Prisma's interactive transactions in test mode.

### Regression tests

- Triggered automatically after:
  - Refactors touching > 5 files
  - Schema changes
  - Dependency major bumps
  - Renames or moves of public symbols
- Suite must cover every "happy path" feature that already works in production.
- Stored under `tests/regression/`. Tagged so they can be invoked with `npm test -- --regression`.
- The self-healing loop in Jarvis runs regression tests automatically after `refactor`, `migrate`, `redesign`, or `architecture` keywords in the task.

## Test-Driven Development (TDD)

Follow the red-green-refactor cycle for all new features:

1. **Write the test first** - define the expected behaviour before writing implementation code.
2. **Red** - run the test and confirm it fails (this validates the test is testing something real).
3. **Green** - write the minimum code to make the test pass. No premature optimisation.
4. **Refactor** - clean up implementation and tests while keeping them green.

### Test requirements

- Every new function/method requires at minimum: one happy-path test, one edge-case test, one error-case test.
- Tests must be deterministic - no random data, no time-dependent assertions without mocking.
- Tests must be isolated - mock external services (APIs, databases, filesystem) unless it is an integration test.
- Group tests by feature using `describe` blocks (JS) or test classes (PHP/Python).

### Test runners by stack

| Stack | Runner | Config |
|-------|--------|--------|
| TypeScript/React | Vitest | `vitest.config.ts` |
| PHP | PHPUnit 11 | `phpunit.xml` |
| Python | pytest | `pyproject.toml` |

### Test file naming

- TypeScript: `src/features/auth/__tests__/login.test.ts`
- PHP: `tests/Unit/AuthServiceTest.php`, `tests/Feature/LoginTest.php`
- Python: `tests/test_auth.py`

### Test suite structure (TypeScript example)

```typescript
import { describe, it, expect, vi, beforeEach } from 'vitest'

describe('AuthService', () => {
  describe('login()', () => {
    beforeEach(() => { /* reset mocks */ })

    it('returns a session token when credentials are valid', async () => { /* ... */ })
    it('throws InvalidCredentialsError when password is wrong', async () => { /* ... */ })
    it('throws AccountLockedError after five failed attempts', async () => { /* ... */ })
  })
})
```

---

## OWASP Security Best Practices

Apply all OWASP Top 10 mitigations by default. Never assume a shortcut is safe.

### Authentication and Authorisation

- Hash passwords with **bcrypt** (cost >= 12) or **Argon2id**. Never MD5, SHA1, or plain text.
- Use **short-lived JWTs** (15 min access token) plus **rotating refresh tokens** stored httpOnly.
- Enforce principle of least privilege - check authorisation on every request, not just on login.
- Rate-limit login, password reset, and any other credential endpoints (max 5 attempts per 15 min per IP).

### Input and Output

- **Validate all input** server-side, regardless of client-side validation.
- Sanitise before rendering any user-provided content to HTML (prevent XSS).
- Use parameterised queries everywhere - never string-interpolate into SQL.
- Escape output contextually: HTML entities for HTML, JSON encoding for JSON, etc.

### Headers and Transport

- Set security headers on every response:
  ```
  Content-Security-Policy: default-src 'self'
  X-Content-Type-Options: nosniff
  X-Frame-Options: DENY
  Referrer-Policy: strict-origin-when-cross-origin
  Strict-Transport-Security: max-age=31536000; includeSubDomains
  ```
- HTTPS everywhere in production. Reject mixed content.
- Configure CORS to allow only known origins - never `*` in production.

### Secrets and Configuration

- **Never hardcode credentials, API keys, connection strings, or tokens** in source code.
- All secrets go in environment variables. Read them at runtime.
- Rotate secrets if they are ever accidentally committed. Treat the commit as a breach.
- Do not log sensitive data (passwords, tokens, card numbers, PII).

### CSRF

- Use CSRF tokens on all state-changing form submissions.
- Validate `Origin` and `Referer` headers on API endpoints that accept cookies.
- Prefer SameSite=Strict or SameSite=Lax cookies.

### Dependencies

- Run `npm audit --audit-level=high` (JS) or `composer audit` (PHP) before every commit.
- Do not use packages with known critical vulnerabilities.
- Pin major versions; let Renovate manage minor/patch updates.

---

## UI/UX Design System

### 60:30:10 Colour Rule

Apply the 60:30:10 ratio to every layout:

| Role | Proportion | Use |
|------|-----------|-----|
| **Primary (dominant)** | 60% | Backgrounds, large surfaces, page canvas |
| **Secondary (complement)** | 30% | Cards, sidebars, section dividers, text |
| **Accent (pop)** | 10% | CTAs, highlights, links, icons, key brand moments |

- Define colours as CSS custom properties in `@theme {}` (Tailwind 4 CSS-first).
- Never scatter literal hex values throughout components - always reference the design token.
- Ensure WCAG AA contrast ratio (4.5:1 for normal text, 3:1 for large text) across all combinations.

### Typography

- Use a type scale: define `--font-size-*` tokens, not arbitrary `text-[17px]`.
- Line height: 1.5 for body, 1.2-1.3 for headings.
- Max line length: 60-80 characters (use `max-w-prose` or equivalent).
- British English copy always (colour, centre, behaviour).

### Spacing and Layout

- Use an 8px base grid. All spacing values should be multiples of 8 (or 4 for tight contexts).
- Mobile-first responsive design. Test at 375px, 768px, 1280px, 1440px.
- Prefer CSS Grid for page layout, Flexbox for component-level alignment.

### Accessibility

- All interactive elements must have a visible focus ring.
- Images require descriptive `alt` text.
- Use semantic HTML: `<nav>`, `<main>`, `<article>`, `<section>`, `<button>`, not `<div>` for everything.
- Keyboard navigation must work throughout the entire page.

---

## Animations - AOS, GSAP, and ScrollTrigger

### General principles

- Animations must **enhance** the content, never obstruct or delay it.
- Respect `prefers-reduced-motion` - disable or reduce all motion when set.
- Keep animation durations between 200ms (micro-interactions) and 800ms (entrance animations).
- Use `ease-out` for entrances, `ease-in` for exits, `ease-in-out` for loops.

### AOS (Animate On Scroll)

Use AOS for simple scroll-triggered entrance animations on content blocks:

```typescript
import AOS from 'aos'
import 'aos/dist/aos.css'

AOS.init({
  duration: 600,
  easing: 'ease-out-cubic',
  once: true,       // animate once only - better performance
  offset: 80,       // trigger 80px before element enters viewport
  disable: window.matchMedia('(prefers-reduced-motion: reduce)').matches,
})
```

HTML attributes:
```html
<section data-aos="fade-up" data-aos-delay="100">...</section>
<div data-aos="fade-right" data-aos-duration="800">...</div>
```

### GSAP + ScrollTrigger

Use GSAP for complex, sequenced, or scrub-linked animations:

```typescript
import gsap from 'gsap'
import { ScrollTrigger } from 'gsap/ScrollTrigger'

gsap.registerPlugin(ScrollTrigger)

// Hero entrance - staggered children
gsap.from('.hero-content > *', {
  opacity: 0,
  y: 30,
  duration: 0.7,
  stagger: 0.12,
  ease: 'power2.out',
})

// Scrub-linked parallax
gsap.to('.parallax-bg', {
  yPercent: -20,
  ease: 'none',
  scrollTrigger: {
    trigger: '.parallax-section',
    start: 'top bottom',
    end: 'bottom top',
    scrub: true,
  },
})

// Section reveal with pin
gsap.timeline({
  scrollTrigger: {
    trigger: '.feature-section',
    start: 'top top',
    end: '+=400',
    scrub: 1,
    pin: true,
  },
})
.from('.feature-card', { opacity: 0, y: 60, stagger: 0.2 })
```

### GSAP best practices

- Always clean up on unmount: `ScrollTrigger.getAll().forEach(t => t.kill())`
- Use `gsap.context()` in React components to scope and auto-clean animations.
- Avoid animating `width`, `height`, or `margin` - use `transform` and `opacity` for GPU compositing.
- Set `will-change: transform` on heavily animated elements.
- Batch `ScrollTrigger.refresh()` calls - call once after all DOM mutations, not per-element.

### React + GSAP pattern

```typescript
import { useEffect, useRef } from 'react'
import gsap from 'gsap'
import { ScrollTrigger } from 'gsap/ScrollTrigger'

gsap.registerPlugin(ScrollTrigger)

export function AnimatedSection({ children }: { children: React.ReactNode }) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const ctx = gsap.context(() => {
      gsap.from(ref.current!.children, {
        opacity: 0,
        y: 40,
        stagger: 0.1,
        duration: 0.6,
        ease: 'power2.out',
        scrollTrigger: {
          trigger: ref.current,
          start: 'top 80%',
        },
      })
    }, ref)

    return () => ctx.revert()  // cleanup on unmount
  }, [])

  return <div ref={ref}>{children}</div>
}
```

---

## Quality Checks - Run Before Every Commit

```bash
# Spell check (British English enforced)
cspell "**/*.{ts,tsx,js,jsx,php,md}" --config /workspace/agent/cspell.config.yaml

# Dependency audit
npm audit --audit-level=high       # JS projects
composer audit                      # PHP projects

# Type check
tsc --noEmit                        # TypeScript projects

# Tests - must all pass
npm test -- --run                   # Vitest
./vendor/bin/phpunit                # PHPUnit
python -m pytest -x                 # pytest

# Lint
npx eslint src --max-warnings 0     # JS/TS
```

---

## General

- **No console.log/var_dump left in committed code** - use proper logging.
- **Environment variables** for all secrets and config - never hardcode.
- **Git**: conventional commits (`feat:`, `fix:`, `chore:` etc). One logical change per commit.
- **Tests**: write tests first (TDD). Minimum three test cases per function (happy path, edge case, error case).
- **No em-dashes** anywhere. No US spellings. British English throughout.

---

## Local Service URLs (inside Docker containers)

| Service | Internal URL | Host URL |
|---------|-------------|----------|
| PHP/Apache | `http://jarvis-php:80` | `http://127.0.0.1:17922` |
| MariaDB | `jarvis-db:3306` | `127.0.0.1:17923` |
| Redis | `jarvis-redis:6379` | `127.0.0.1:17924` |
| vLLM/Qwen | `http://host.docker.internal:18000/v1` | `http://127.0.0.1:18000/v1` |
| XAMPP htdocs | `/workspace/htdocs` | `C:\xampp\htdocs` |
