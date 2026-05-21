"""Stack contract enforcement for delegated tasks.

Jarvis resolves the profile at queue time and stores it in
metadata.local_agent_stack. The runner prepends it to the prompt and
validates the workspace before commit on build-class tasks.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .task_evidence import is_build_class_task


def contract_from_metadata(metadata: dict[str, Any]) -> dict[str, Any] | None:
    raw = metadata.get("local_agent_stack")
    if isinstance(raw, dict) and raw.get("profile_id"):
        return raw
    return None


def format_stack_prompt_block(contract: dict[str, Any]) -> str:
    """XML-ish block prepended after coding standards."""
    payload = json.dumps(contract, indent=2)
    return (
        "<stack_contract trust=\"policy\">\n"
        "This block is authoritative for framework choice. "
        "Do not substitute a different stack unless the user explicitly "
        "overrode it in the task body.\n\n"
        f"{payload}\n"
        "</stack_contract>\n\n"
    )


def _read_package_deps(workspace: Path) -> dict[str, str]:
    pkg = workspace / "package.json"
    if not pkg.exists():
        return {}
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except Exception:
        return {}
    deps: dict[str, str] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        block = data.get(key) or {}
        if isinstance(block, dict):
            for name, ver in block.items():
                if isinstance(name, str):
                    deps[name] = str(ver) if ver is not None else ""
    return deps


def _path_exists(workspace: Path, rel: str) -> bool:
    return (workspace / rel).exists()


def validate_workspace_against_contract(
    workspace: Path,
    contract: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Return (ok, list of failure messages). Empty contract skips checks."""
    profile = contract.get("profile_id") or ""
    if profile in ("inherit-repo", "ops-bash", "python-fastapi", ""):
        return True, []

    failures: list[str] = []

    deps = _read_package_deps(workspace)
    for name in contract.get("required_deps") or []:
        if isinstance(name, str) and name not in deps:
            failures.append(f"missing required dependency `{name}` in package.json")

    for name in contract.get("forbidden_deps") or []:
        if isinstance(name, str) and name in deps:
            failures.append(f"forbidden dependency `{name}` present in package.json")

    for group in contract.get("required_path_groups") or []:
        if not isinstance(group, list):
            continue
        paths = [p for p in group if isinstance(p, str)]
        if paths and not any(_path_exists(workspace, p) for p in paths):
            failures.append(
                f"missing required path (need one of): {', '.join(paths)}"
            )

    if profile == "next-static-export":
        for cfg in ("next.config.mjs", "next.config.ts", "next.config.js"):
            p = workspace / cfg
            if p.exists():
                text = p.read_text(encoding="utf-8", errors="replace").lower()
                if "export" not in text and "output" not in text:
                    failures.append(
                        f"{cfg} must set `output: 'export'` for cdv-sites-static deploy"
                    )
                break

    if profile == "astro-static-demo":
        src = workspace / "src"
        if src.is_dir():
            source_files = [
                f for f in src.rglob("*")
                if f.is_file() and f.suffix in {".astro", ".tsx", ".jsx", ".vue", ".ts", ".js"}
            ]
            if len(source_files) < 2:
                failures.append(
                    "scaffold looks incomplete — expected multiple source files under src/"
                )
        else:
            failures.append("missing src/ directory for Astro scaffold")

    elif profile == "react-vite-spa":
        src = workspace / "src"
        if src.is_dir():
            source_files = [
                f for f in src.rglob("*")
                if f.is_file() and f.suffix in {".tsx", ".jsx", ".vue", ".ts", ".js"}
            ]
            if len(source_files) < 2:
                failures.append(
                    "scaffold looks incomplete — expected multiple source files under src/"
                )
        else:
            failures.append("missing src/ directory for Vite scaffold")

    elif profile == "next-static-export":
        # App Router (app/) or Pages Router (pages/) — not src/ like Vite/Astro.
        page_roots: list[Path] = []
        for name in ("app", "pages"):
            root = workspace / name
            if root.is_dir():
                page_roots.append(root)
        if not page_roots:
            failures.append(
                "missing app/ or pages/ directory for Next.js static export scaffold"
            )
        else:
            page_files = [
                f for root in page_roots for f in root.rglob("*")
                if f.is_file()
                and f.suffix in {".tsx", ".jsx", ".ts", ".js"}
                and (f.name.startswith("page") or f.name in {"index.tsx", "index.jsx"})
            ]
            if not page_files:
                failures.append(
                    "expected at least one route file (e.g. app/page.tsx or pages/index.tsx)"
                )

    return len(failures) == 0, failures


def format_stack_failure_message(
    failures: list[str],
    profile_id: str | None = None,
) -> str:
    """Human-readable stack_mismatch message for Postgres + UI."""
    label = profile_id or "stack contract"
    if not failures:
        return f"Workspace does not match the resolved {label}."
    detail = "; ".join(failures)
    return f"Workspace does not match {label}: {detail}"


def pre_scaffold_workspace(
    workspace: Path,
    contract: dict[str, Any],
    repo_name: str,
) -> list[str]:
    """Write a minimal skeleton matching the profile.

    Called by the runner BEFORE handing off to Claude so that:
      1. Claude has a real starting point instead of a blank repo, and
      2. The stack-policy validator can find the required-path files
         even if Claude under-delivers (defensive).

    The scaffold is intentionally minimal — Claude is expected to
    extend / rewrite it. We avoid `npm create <framework>` (network +
    interactive) and write files directly. No-ops for profiles whose
    starting point is the existing repo (`inherit-repo`, `ops-bash`,
    `python-fastapi`).

    Returns the list of files written (relative paths). Empty list
    means no scaffold was needed or the profile is not supported.
    """
    profile = contract.get("profile_id") or ""
    if profile in ("inherit-repo", "ops-bash", "python-fastapi", ""):
        return []
    # Skip if the workspace already looks scaffolded — never overwrite
    # an existing project.
    if (workspace / "package.json").exists():
        return []

    if profile == "astro-static-demo":
        return _scaffold_astro(workspace, repo_name)
    if profile == "next-static-export":
        return _scaffold_next_static_export(workspace, repo_name)
    if profile == "react-vite-spa":
        return _scaffold_react_vite_spa(workspace, repo_name)
    return []


def _write(workspace: Path, rel: str, content: str) -> str:
    target = workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return rel


def _scaffold_astro(workspace: Path, repo_name: str) -> list[str]:
    """Minimal Astro 5 + Tailwind 4 scaffold. Claude fills the content."""
    written: list[str] = []
    written.append(_write(workspace, "package.json", json.dumps({
        "name": repo_name,
        "type": "module",
        "private": True,
        "version": "0.0.1",
        "scripts": {
            "dev": "astro dev",
            "build": "astro build",
            "preview": "astro preview",
            "astro": "astro",
        },
        "dependencies": {
            "astro": "^5.0.0",
            "@astrojs/tailwind": "^6.0.0",
            "tailwindcss": "^4.0.0",
            "lucide-astro": "^0.0.1",
        },
        "devDependencies": {
            "typescript": "^6.0.0",
            "@types/node": "^24.0.0",
        },
    }, indent=2) + "\n"))
    written.append(_write(workspace, "astro.config.mjs", (
        "import { defineConfig } from 'astro/config'\n"
        "import tailwind from '@astrojs/tailwind'\n\n"
        "export default defineConfig({\n"
        "  integrations: [tailwind()],\n"
        "  output: 'static',\n"
        "})\n"
    )))
    written.append(_write(workspace, "tsconfig.json", json.dumps({
        "extends": "astro/tsconfigs/strict",
        "compilerOptions": {
            "strict": True,
            "jsx": "preserve",
        },
        "include": ["src/**/*", "astro.config.mjs"],
    }, indent=2) + "\n"))
    written.append(_write(workspace, "src/pages/index.astro", (
        "---\n"
        "// Replace this stub. Build the site requested in the task body.\n"
        "const title = 'Coming soon'\n"
        "---\n"
        "<html lang=\"en-GB\">\n"
        "  <head>\n"
        "    <meta charset=\"utf-8\" />\n"
        "    <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\" />\n"
        "    <title>{title}</title>\n"
        "  </head>\n"
        "  <body>\n"
        "    <main><h1>{title}</h1></main>\n"
        "  </body>\n"
        "</html>\n"
    )))
    written.append(_write(workspace, "src/layouts/Base.astro", (
        "---\n"
        "interface Props { title: string }\n"
        "const { title } = Astro.props\n"
        "---\n"
        "<html lang=\"en-GB\">\n"
        "  <head>\n"
        "    <meta charset=\"utf-8\" />\n"
        "    <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\" />\n"
        "    <title>{title}</title>\n"
        "  </head>\n"
        "  <body><slot /></body>\n"
        "</html>\n"
    )))
    written.append(_write(workspace, ".gitignore", (
        "node_modules\ndist\n.astro\n.env\n.env.*\n!.env.example\n.DS_Store\n"
    )))
    return written


def _scaffold_next_static_export(workspace: Path, repo_name: str) -> list[str]:
    """Next.js 16 App Router with output:'export' for static deploy."""
    written: list[str] = []
    written.append(_write(workspace, "package.json", json.dumps({
        "name": repo_name,
        "private": True,
        "version": "0.0.1",
        "scripts": {
            "dev": "next dev",
            "build": "next build",
            "start": "next start",
        },
        "dependencies": {
            "next": "^16.0.0",
            "react": "^19.0.0",
            "react-dom": "^19.0.0",
            "tailwindcss": "^4.0.0",
            "lucide-react": "^0.0.1",
        },
        "devDependencies": {
            "typescript": "^6.0.0",
            "@types/node": "^24.0.0",
            "@types/react": "^19.0.0",
        },
    }, indent=2) + "\n"))
    written.append(_write(workspace, "next.config.mjs", (
        "/** @type {import('next').NextConfig} */\n"
        "const nextConfig = {\n"
        "  output: 'export',\n"
        "  images: { unoptimized: true },\n"
        "}\n"
        "export default nextConfig\n"
    )))
    written.append(_write(workspace, "tsconfig.json", json.dumps({
        "compilerOptions": {
            "target": "ES2022",
            "lib": ["dom", "dom.iterable", "esnext"],
            "allowJs": True,
            "skipLibCheck": True,
            "strict": True,
            "noEmit": True,
            "esModuleInterop": True,
            "module": "esnext",
            "moduleResolution": "bundler",
            "resolveJsonModule": True,
            "isolatedModules": True,
            "jsx": "preserve",
            "incremental": True,
            "plugins": [{"name": "next"}],
            "paths": {"@/*": ["./*"]},
        },
        "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx"],
        "exclude": ["node_modules"],
    }, indent=2) + "\n"))
    written.append(_write(workspace, "app/layout.tsx", (
        "import type { ReactNode } from 'react'\n"
        "import './globals.css'\n\n"
        "export const metadata = { title: 'Coming soon' }\n\n"
        "export default function RootLayout({ children }: { children: ReactNode }) {\n"
        "  return (\n"
        "    <html lang=\"en-GB\">\n"
        "      <body>{children}</body>\n"
        "    </html>\n"
        "  )\n"
        "}\n"
    )))
    written.append(_write(workspace, "app/page.tsx", (
        "// Replace this stub. Build the site requested in the task body.\n"
        "export default function HomePage() {\n"
        "  return (\n"
        "    <main>\n"
        "      <h1>Coming soon</h1>\n"
        "    </main>\n"
        "  )\n"
        "}\n"
    )))
    written.append(_write(workspace, "app/globals.css", (
        "@import 'tailwindcss';\n\n"
        ":root { color-scheme: light dark; }\n"
        "body { font-family: system-ui, -apple-system, sans-serif; }\n"
    )))
    written.append(_write(workspace, ".gitignore", (
        "node_modules\n.next\nout\ndist\n.env\n.env.*\n!.env.example\n.DS_Store\n"
    )))
    return written


def _scaffold_react_vite_spa(workspace: Path, repo_name: str) -> list[str]:
    """Vite 8 + React 19 single-page app."""
    written: list[str] = []
    written.append(_write(workspace, "package.json", json.dumps({
        "name": repo_name,
        "type": "module",
        "private": True,
        "version": "0.0.1",
        "scripts": {
            "dev": "vite",
            "build": "tsc -b && vite build",
            "preview": "vite preview",
        },
        "dependencies": {
            "react": "^19.0.0",
            "react-dom": "^19.0.0",
            "lucide-react": "^0.0.1",
        },
        "devDependencies": {
            "vite": "^8.0.0",
            "@vitejs/plugin-react": "^5.0.0",
            "typescript": "^6.0.0",
            "@types/react": "^19.0.0",
            "@types/react-dom": "^19.0.0",
            "tailwindcss": "^4.0.0",
            "@tailwindcss/vite": "^4.0.0",
        },
    }, indent=2) + "\n"))
    written.append(_write(workspace, "vite.config.ts", (
        "import { defineConfig } from 'vite'\n"
        "import react from '@vitejs/plugin-react'\n"
        "import tailwindcss from '@tailwindcss/vite'\n\n"
        "export default defineConfig({\n"
        "  plugins: [react(), tailwindcss()],\n"
        "})\n"
    )))
    written.append(_write(workspace, "tsconfig.json", json.dumps({
        "compilerOptions": {
            "target": "ES2022",
            "useDefineForClassFields": True,
            "lib": ["ES2022", "DOM", "DOM.Iterable"],
            "module": "ESNext",
            "skipLibCheck": True,
            "moduleResolution": "bundler",
            "allowImportingTsExtensions": True,
            "resolveJsonModule": True,
            "isolatedModules": True,
            "noEmit": True,
            "jsx": "react-jsx",
            "strict": True,
        },
        "include": ["src"],
    }, indent=2) + "\n"))
    written.append(_write(workspace, "index.html", (
        "<!doctype html>\n"
        "<html lang=\"en-GB\">\n"
        "  <head>\n"
        "    <meta charset=\"utf-8\" />\n"
        "    <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\" />\n"
        "    <title>Coming soon</title>\n"
        "  </head>\n"
        "  <body>\n"
        "    <div id=\"root\"></div>\n"
        "    <script type=\"module\" src=\"/src/main.tsx\"></script>\n"
        "  </body>\n"
        "</html>\n"
    )))
    written.append(_write(workspace, "src/main.tsx", (
        "import { StrictMode } from 'react'\n"
        "import { createRoot } from 'react-dom/client'\n"
        "import App from './App'\n"
        "import './index.css'\n\n"
        "createRoot(document.getElementById('root')!).render(\n"
        "  <StrictMode><App /></StrictMode>,\n"
        ")\n"
    )))
    written.append(_write(workspace, "src/App.tsx", (
        "// Replace this stub. Build the SPA requested in the task body.\n"
        "export default function App() {\n"
        "  return <main><h1>Coming soon</h1></main>\n"
        "}\n"
    )))
    written.append(_write(workspace, "src/index.css", (
        "@import 'tailwindcss';\n"
    )))
    written.append(_write(workspace, ".gitignore", (
        "node_modules\ndist\n.env\n.env.*\n!.env.example\n.DS_Store\n"
    )))
    return written


def should_validate_stack(task: dict[str, Any], metadata: dict[str, Any]) -> bool:
    """Run stack validation on build-class tasks with a resolved profile."""
    contract = contract_from_metadata(metadata)
    if not contract:
        return False
    profile = contract.get("profile_id")
    if profile in ("inherit-repo", "ops-bash", None):
        return False
    return is_build_class_task(task)
