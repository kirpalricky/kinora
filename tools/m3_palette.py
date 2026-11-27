#!/usr/bin/env python3
"""Dev-time generator for kinora/web/static/m3.css. Not shipped, not imported by the app.

Seed #1F305E. Material 3 tonal palettes and colour roles (HCT, spec 2021) come from the
PyPI package `materialyoucolor` (dev-only dependency). The semantic ok/warn colours are
not in M3 and are fixed per theme here. The output is committed; the app and the tests
never run this script.

Run:  .venv/bin/python tools/m3_palette.py      then  .venv/bin/python tools/check_m3_contrast.py
"""
from pathlib import Path

from materialyoucolor.dynamiccolor.material_dynamic_colors import MaterialDynamicColors as M
from materialyoucolor.hct import Hct
from materialyoucolor.scheme.scheme_tonal_spot import SchemeTonalSpot

SEED = 0xFF1F305E
SPEC = "2021"
OUT = Path(__file__).resolve().parent.parent / "kinora" / "web" / "static" / "m3.css"

TONES = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 99, 100]
PALETTES = [  # css name, attribute on the scheme
    ("primary", "primary_palette"),
    ("secondary", "secondary_palette"),
    ("tertiary", "tertiary_palette"),
    ("neutral", "neutral_palette"),
    ("neutral-variant", "neutral_variant_palette"),
    ("error", "error_palette"),
]

# M3 colour roles (css name without the --md-sys-color- prefix); the attribute is the same name with _.
ROLES = [
    "primary", "on-primary", "primary-container", "on-primary-container",
    "secondary", "on-secondary", "secondary-container", "on-secondary-container",
    "tertiary", "on-tertiary", "tertiary-container", "on-tertiary-container",
    "error", "on-error", "error-container", "on-error-container",
    "background", "on-background", "surface", "on-surface", "surface-variant", "on-surface-variant",
    "surface-dim", "surface-bright", "surface-container-lowest", "surface-container-low",
    "surface-container", "surface-container-high", "surface-container-highest",
    "outline", "outline-variant", "inverse-surface", "inverse-on-surface", "inverse-primary",
    "scrim", "shadow",
]

# Semantic status colours (not M3): text colour and soft container background, per theme.
SEMANTIC = {
    "light": {"ok": "#1F7A4D", "ok-container": "#E6F4EC", "warn": "#8A5300", "warn-container": "#FBF0DC"},
    "dark": {"ok": "#4CC38A", "ok-container": "#15291F", "warn": "#E8B04B", "warn-container": "#2D2413"},
}

# Existing tokens in kinora/web/index.html mapped onto the new roles (compatibility aliases).
ALIASES = [
    ("--c-bg", "background"),
    ("--c-surface", "surface"),
    ("--c-surface-2", "surface-container-high"),
    ("--c-line", "outline-variant"),
    ("--c-line-strong", "outline"),
    ("--c-text", "on-surface"),
    ("--c-text-2", "on-surface-variant"),
    ("--c-text-3", "on-surface-variant"),
    ("--c-accent", "primary"),
    ("--c-accent-hover", "primary"),
    ("--c-accent-text", "primary"),
    ("--c-accent-soft", "primary-container"),
    ("--c-on-accent", "on-primary"),
    ("--c-bad", "error"),
    ("--c-bad-soft", "error-container"),
    ("--track", "surface-container-high"),
    ("--rec", "outline"),
]
SEMANTIC_ALIASES = [
    ("--c-ok", "ok"), ("--c-ok-soft", "ok-container"),
    ("--c-warn", "warn"), ("--c-warn-soft", "warn-container"),
]

TYPE = [  # role, size px, line-height px, weight, tracking px
    ("display-large", 57, 64, 400, -0.25), ("display-medium", 45, 52, 400, 0), ("display-small", 36, 44, 400, 0),
    ("headline-large", 32, 40, 400, 0), ("headline-medium", 28, 36, 400, 0), ("headline-small", 24, 32, 400, 0),
    ("title-large", 22, 28, 400, 0), ("title-medium", 16, 24, 500, 0.15), ("title-small", 14, 20, 500, 0.1),
    ("body-large", 16, 24, 400, 0.5), ("body-medium", 14, 20, 400, 0.25), ("body-small", 12, 16, 400, 0.4),
    ("label-large", 14, 20, 500, 0.1), ("label-medium", 12, 16, 500, 0.5), ("label-small", 11, 16, 500, 0.5),
]
SHAPE = [("none", "0px"), ("extra-small", "4px"), ("small", "8px"), ("medium", "12px"),
         ("large", "16px"), ("extra-large", "28px"), ("full", "9999px")]
# tonal elevation: surface tint opacity per level (M3 levels 0-5)
ELEVATION = [0, 0.05, 0.08, 0.11, 0.12, 0.14]
EASING = [  # name, cubic-bezier
    ("emphasized", "0.2,0,0,1"), ("emphasized-decelerate", "0.05,0.7,0.1,1"),
    ("emphasized-accelerate", "0.3,0,0.8,0.15"), ("standard", "0.2,0,0,1"),
    ("standard-decelerate", "0,0,0,1"), ("standard-accelerate", "0.3,0,1,1"),
]
DURATIONS = [("short1", 50), ("short2", 100), ("short3", 150), ("short4", 200), ("medium1", 250),
             ("medium2", 300), ("medium3", 350), ("medium4", 400), ("long1", 450), ("long2", 500)]
STATE = [("hover", ".08"), ("focus", ".10"), ("pressed", ".10"), ("dragged", ".16")]


def hex_of(argb):
    return f"#{argb & 0xFFFFFF:06x}"


def scheme_roles(is_dark):
    s = SchemeTonalSpot(Hct.from_int(SEED), is_dark, 0.0, spec_version=SPEC)
    roles = {r: hex_of(getattr(M, r.replace("-", "_")).get_argb(s)) for r in ROLES}
    return s, roles


def decl_lines(pairs, indent="  "):
    return [f"{indent}{k}:{v};" for k, v in pairs]


def role_pairs(roles, semantic):
    pairs = [(f"--md-sys-color-{r}", roles[r]) for r in ROLES]
    pairs += [(f"--md-sys-color-{k}", v.lower()) for k, v in semantic.items()]
    return pairs


def build():
    light_scheme, light = scheme_roles(False)
    _, dark = scheme_roles(True)
    out = []
    out.append("/* generated by tools/m3_palette.py, do not edit. */")
    out.append(f"/* Seed {hex_of(SEED)}; Material 3 tonal palettes and roles (materialyoucolor, spec {SPEC}); "
               "semantic ok/warn are fixed (not M3). */")
    out.append("/* Not loaded by index.html yet. Regenerate: .venv/bin/python tools/m3_palette.py */")
    out.append("")

    # light theme (default), with reference palettes, type, shape, elevation, motion, state layers
    out.append(":root{color-scheme:light;")
    out.append("  /* reference palettes (HCT tones) */")
    for name, attr in PALETTES:
        pal = getattr(light_scheme, attr)
        out += decl_lines([(f"--md-ref-palette-{name}{t}", hex_of(pal.tone(t))) for t in TONES])
    out.append("  /* colour roles, light */")
    out += decl_lines(role_pairs(light, SEMANTIC["light"]))
    out.append("  /* typography scale (size, line height, weight, tracking) */")
    for role, size, lh, wt, tr in TYPE:
        out += decl_lines([
            (f"--md-sys-typescale-{role}-size", f"{size}px"),
            (f"--md-sys-typescale-{role}-line-height", f"{lh}px"),
            (f"--md-sys-typescale-{role}-weight", str(wt)),
            (f"--md-sys-typescale-{role}-tracking", f"{tr:g}px"),
        ])
    out.append("  /* shape scale */")
    out += decl_lines([(f"--md-sys-shape-corner-{n}", v) for n, v in SHAPE])
    out.append("  /* tonal elevation levels (surface tint opacity) */")
    out += decl_lines([(f"--md-sys-elevation-{i}", f"{v:g}") for i, v in enumerate(ELEVATION)])
    out.append("  /* motion: easing and duration (reduced motion below) */")
    out += decl_lines([(f"--md-sys-motion-easing-{n}", f"cubic-bezier({c})") for n, c in EASING])
    out += decl_lines([(f"--md-sys-motion-duration-{n}", f"{ms}ms") for n, ms in DURATIONS])
    out.append("  /* state layer opacities */")
    out += decl_lines([(f"--md-sys-state-{n}", v) for n, v in STATE])
    out.append("  /* window size classes: compact < 600px; medium 600-839px; expanded >= 840px */")
    out.append("}")

    # dark theme, the same mechanism index.html uses today
    dark_decls = role_pairs(dark, SEMANTIC["dark"])
    out.append('@media(prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;')
    out += decl_lines(dark_decls)
    out.append("}}")
    out.append(':root[data-theme="dark"]{color-scheme:dark;')
    out += decl_lines(dark_decls)
    out.append("}")
    out.append(':root[data-theme="light"]{color-scheme:light}')
    out.append("")

    # reduced motion: durations become 0
    out.append("@media (prefers-reduced-motion:reduce){:root{")
    out += decl_lines([(f"--md-sys-motion-duration-{n}", "0ms") for n, _ in DURATIONS])
    out.append("}}")
    out.append("")

    # compatibility aliases for the tokens index.html uses today (index.html does not load this file yet)
    out.append("/* compatibility aliases: the tokens in index.html mapped onto the M3 roles */")
    out.append(":root{")
    out += decl_lines([(old, f"var(--md-sys-color-{new})") for old, new in ALIASES + SEMANTIC_ALIASES])
    out.append("}")
    return "\n".join(out) + "\n"


def main():
    OUT.write_text(build(), encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
