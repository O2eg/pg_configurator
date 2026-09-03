#!/usr/bin/env python3
"""Build the self-contained pg-configurator page from the template.

Writes two identical copies: ``web/dist/pg-configurator.html`` (the release and
Pages artifact) and ``web/pg-configurator.html`` — the page opened while
working. Both are build output and untracked. A page that links to external
CSS and scripts cannot read them from ``file://``, so the working copy has to
be self-contained as well.

Inlined: the two stylesheets, the three generated data files, and the ES
modules the page needs.

**Module bundling without a bundler.** The sources are ES modules, but a page
cannot resolve relative imports inside an inline ``<script type="module">``.
Rather than add a bundler dependency to a repository that currently needs no
npm install at all, the modules are concatenated in dependency order with their
`import`/`export` statements stripped — they share one scope afterwards. That
is only safe while no two modules declare the same top-level name, so the build
checks exactly that and refuses to write a page when it is violated. If the
module graph ever outgrows this, esbuild pinned in the lockfile is the upgrade.

Nothing external remains: the result works from ``file://`` offline and makes
no network request.
"""

from __future__ import annotations

import pathlib
import re
import sys

WEB = pathlib.Path(__file__).resolve().parent
ROOT = WEB.parent
DIST = WEB / "dist"

# Dependency order. Every import between these files must point backwards.
MODULES = (
    "src/field-help.js",
    "src/units.js",
    "src/rule-eval.js",
    "src/configurator.js",
    "src/make-conf.js",
    "src/config-diff.js",
    "src/render.js",
)

DATA = (
    ("PC:DATA-RULES", "pgc-rules", "data/rules.json"),
    ("PC:DATA-SETTINGS", "pgc-pg-settings", "data/pg_settings.json"),
    ("PC:DATA-SCHEMA", "pgc-input-schema", "data/input-schema.json"),
)

STYLES = (
    ("PC:CSS-THEME", "css/pgc-theme.css"),
    ("PC:CSS-MAIN", "css/pgc.css"),
)

IMPORT_RE = re.compile(r"^import\s[^;]*?;\s*$", re.MULTILINE | re.DOTALL)
EXPORT_RE = re.compile(r"^export\s+(?=(?:const|let|var|function|class|async)\b)", re.MULTILINE)
EXPORT_LIST_RE = re.compile(r"^export\s*\{[^}]*\}\s*;?\s*$", re.MULTILINE)
DECLARATION_RE = re.compile(
    r"^(?:export\s+)?(?:async\s+)?(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)


def read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def strip_module_syntax(source: str) -> str:
    """Turn one ES module into a fragment that shares the surrounding scope."""

    source = IMPORT_RE.sub("", source)
    source = EXPORT_LIST_RE.sub("", source)
    return EXPORT_RE.sub("", source)


def collect_declarations(source: str) -> set[str]:
    return set(DECLARATION_RE.findall(source))


def bundle() -> str:
    """Concatenate the modules, refusing to hide a name collision."""

    seen: dict[str, str] = {}
    parts = []
    for name in MODULES:
        source = read(WEB / name)
        for declaration in collect_declarations(source):
            if declaration in seen:
                raise SystemExit(
                    f"build: {name} and {seen[declaration]} both declare '{declaration}'. "
                    "Concatenating them would shadow one; rename it or switch to a bundler."
                )
            seen[declaration] = name
        parts.append(f"// --- {name} " + "-" * max(0, 60 - len(name)) + "\n")
        parts.append(strip_module_syntax(source).strip())
        parts.append("\n\n")
    return "".join(parts)


def entry_point(template_script: str) -> str:
    """The page's own inline script, with its import of render.js removed."""

    return IMPORT_RE.sub("", template_script).strip()


def main() -> int:
    html = read(WEB / "configurator.template.html")

    for marker, path in STYLES:
        css = read(WEB / path)
        pattern = re.compile(r"<link[^>]*>\s*<!--" + re.escape(marker) + r"-->")
        if not pattern.search(html):
            raise SystemExit(f"build: marker {marker} not found in the template")
        replacement = f"<style>\n{css}\n</style>"
        html = pattern.sub(lambda _, value=replacement: value, html, count=1)

    for marker, element_id, path in DATA:
        payload = read(WEB / path).strip()
        # A JSON island ends at the first `</script>` in the byte stream, and a
        # lone `<` would still be parsed as markup by a lenient reader.
        if "</script" in payload.lower():
            raise SystemExit(f"build: {path} contains a closing script tag")
        payload = payload.replace("<", "\\u003c").replace("\u2028", "\\u2028")
        payload = payload.replace("\u2029", "\\u2029")
        pattern = re.compile(
            r'<script type="application/json" id="'
            + re.escape(element_id)
            + r'">.*?</script><!--'
            + re.escape(marker)
            + r"-->",
            re.DOTALL,
        )
        if not pattern.search(html):
            raise SystemExit(f"build: marker {marker} not found in the template")
        replacement = f'<script type="application/json" id="{element_id}">{payload}</script>'
        html = pattern.sub(lambda _, value=replacement: value, html, count=1)

    script_pattern = re.compile(r'<script type="module"><!--PC:JS-CORE-->(.*?)</script>', re.DOTALL)
    match = script_pattern.search(html)
    if match is None:
        raise SystemExit("build: marker PC:JS-CORE not found in the template")
    combined = bundle() + entry_point(match.group(1)) + "\n"
    if "</script" in combined.lower():
        raise SystemExit("build: a module contains a closing script tag")
    html = script_pattern.sub(
        lambda _: f'<script type="module">\n{combined}</script>', html, count=1
    )

    leftovers = re.findall(r"<!--PC:[A-Z-]+-->", html)
    if leftovers:
        raise SystemExit(f"build: unresolved markers: {', '.join(sorted(set(leftovers)))}")
    for external in ('src="http', 'href="http://', 'href="https://fonts'):
        if external in html:
            raise SystemExit(f"build: the page still references an external resource: {external}")

    DIST.mkdir(parents=True, exist_ok=True)
    for target in (DIST / "pg-configurator.html", WEB / "pg-configurator.html"):
        target.write_text(html, encoding="utf-8")
        print(f"build: wrote {target.relative_to(ROOT)} ({len(html):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
