#!/usr/bin/env python3
"""WCAG contrast check for kinora/web/static/m3.css (stdlib only, dev-time).

Checks text pairs in both themes (light, dark via the media query and via data-theme="dark")
at >= 4.5:1, and outline against surface at >= 3:1. Prints a table; exits 1 on any failure.
Run:  .venv/bin/python tools/check_m3_contrast.py
"""
import re
import sys
from pathlib import Path

CSS = Path(__file__).resolve().parent.parent / "kinora" / "web" / "static" / "m3.css"

TEXT_MIN = 4.5
NONTEXT_MIN = 3.0

SURFACES = ["surface", "surface-container-lowest", "surface-container-low", "surface-container",
            "surface-container-high", "surface-container-highest"]
PAIRS = []  # (fg, bg, minimum, label)
for a, b in [("primary", "primary"), ("primary-container", "primary-container"),
             ("secondary", "secondary"), ("secondary-container", "secondary-container"),
             ("tertiary", "tertiary"), ("tertiary-container", "tertiary-container"),
             ("error", "error"), ("error-container", "error-container")]:
    fg = "on-" + a
    PAIRS.append((fg, b, TEXT_MIN))
PAIRS += [("on-surface", s, TEXT_MIN) for s in SURFACES]
PAIRS.append(("on-surface-variant", "surface", TEXT_MIN))
PAIRS.append(("on-surface-variant", "surface-container-highest", TEXT_MIN))
PAIRS.append(("inverse-on-surface", "inverse-surface", TEXT_MIN))
PAIRS += [("ok", "ok-container", TEXT_MIN), ("warn", "warn-container", TEXT_MIN),
          ("error", "error-container", TEXT_MIN)]  # bad = M3 error
PAIRS.append(("outline", "surface", NONTEXT_MIN))

BLOCK = re.compile(r"([^{}]+)\{([^{}]*)\}")
DECL = re.compile(r"(--[\w-]+)\s*:\s*([^;]+);")
VAR = re.compile(r"var\((--[\w-]+)\)")
HEX = re.compile(r"^#([0-9a-fA-F]{6})$")

LIGHT_SEL = {":root"}
DARK_SELS = {
    "media": {":root", ':root:not([data-theme="light"])'},
    "attr": {":root", ':root[data-theme="dark"]'},
}


def parse_blocks(css):
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    blocks = []
    # innermost blocks only: the selector of a @media wrapper is matched as the inner rule's selector
    for sel, body in BLOCK.findall(css):
        blocks.append((sel.strip(), dict(DECL.findall(body))))
    return blocks


def theme_vars(blocks, selectors):
    vars_ = {}
    for sel, decls in blocks:  # file order: later declarations win
        if sel in selectors:
            vars_.update(decls)
    return vars_


def resolve(value, vars_, depth=0):
    if depth > 10:
        raise ValueError("var() cycle")
    value = value.strip()
    m = VAR.fullmatch(value)
    if m:
        return resolve(vars_[m.group(1)], vars_, depth + 1)
    return value


def rgb(value):
    m = HEX.match(value)
    if not m:
        raise ValueError(f"not a 6-digit hex colour: {value!r}")
    h = m.group(1)
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def luminance(c):
    def lin(v):
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(v) for v in c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(c1, c2):
    l1, l2 = sorted((luminance(c1), luminance(c2)), reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)


def main():
    if not CSS.exists():
        print(f"missing {CSS}: run tools/m3_palette.py first")
        return 1
    blocks = parse_blocks(CSS.read_text(encoding="utf-8"))
    themes = {
        "light": theme_vars(blocks, LIGHT_SEL),
        "dark (media)": theme_vars(blocks, DARK_SELS["media"]),
        "dark (data-theme)": theme_vars(blocks, DARK_SELS["attr"]),
    }
    rows, failed = [], 0
    for name, vars_ in themes.items():
        for fg_role, bg_role, minimum in PAIRS:
            fg_name, bg_name = f"--md-sys-color-{fg_role}", f"--md-sys-color-{bg_role}"
            try:
                value = ratio(rgb(resolve(vars_[fg_name], vars_)), rgb(resolve(vars_[bg_name], vars_)))
            except (KeyError, ValueError) as exc:
                rows.append((name, fg_role, bg_role, "-", f">= {minimum}", f"ERROR {exc}"))
                failed += 1
                continue
            ok = value >= minimum
            failed += 0 if ok else 1
            rows.append((name, fg_role, bg_role, f"{value:.2f}", f">= {minimum}", "pass" if ok else "FAIL"))

    headers = ("theme", "foreground", "background", "ratio", "need", "result")
    widths = [max(len(str(r[i])) for r in rows + [headers]) for i in range(len(headers))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for r in rows:
        print(fmt.format(*r))
    print(f"\n{len(rows) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
