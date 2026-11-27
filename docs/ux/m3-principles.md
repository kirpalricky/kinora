# Kinora and Material 3: principles and decisions

Status: DRAFT for owner approval. No UI code for B5 to B7 is written until this page is approved. Every component decision below cites the rule it follows (R1 to R9). A decision that cannot cite a rule does not go in.

## Context
Kinora is a viewer for three private indoor cameras, used on a desktop browser and from a phone on the LAN. The job is: pick a camera, watch live or scrub the recording, export a clip. It is a utility, not a media browser. The UI should be calm, dense enough for a desktop, and thumb-friendly on a phone.

## Principles (what we take from M3, and what we do not)

**R1. One emphasis hierarchy.** M3 orders actions by prominence: filled, tonal, outlined, text. Each surface has at most one filled button (its primary action). Everything else is tonal, outlined or text. Prominence tells the user what to do next; it is never decoration.

**R2. Peer views are tabs; actions are icon buttons.** Content the user switches between (the three cameras) uses primary tabs. Commands (add, theme, settings, refresh, export per event) are buttons; in an app bar they are icon buttons only, all the same style, with tooltips and accessible names. A text-and-icon button in the bar is not allowed.

**R3. Colour roles carry meaning.**
- Primary: the selected tab indicator, the one filled button, focus rings, the playhead.
- Secondary container: a selected choice inside a control (segmented button, chip, selected sheet item).
- Surface containers (lowest to highest): layers. Raised regions use a tonal step, not a border.
- Error / error container: errors only. Warn and OK are Kinora's own semantic colours, used only for status (dots, banners), never for emphasis.
- On-surface-variant: secondary text and unselected icons.

**R4. Same job, same component.** Single choice among a few options is a segmented button (2 to 5 options) or a filter chip row (a variable set). Binary setting is a switch. A choice among many is a menu. Two controls that do the same kind of job look the same everywhere on the page.

**R5. Structure adapts to the window class, not just the spacing.** Compact (under 600 px): single column, camera dropdown plus bottom sheet, events as a bottom sheet. Medium (600 to 839): tabs in the bar, events below the player. Expanded (840 and up): player plus events side by side. We keep the existing 960 px two-column grid rule for the player; M3 classes decide the shell and the controls.

**R6. Surfaces are tonal, not outlined.** Cards are `surface-container` on `surface`; elevation is a tone step. A 1px outline is used only where two same-tone regions touch or for an outlined text field/button, as M3 specifies.

**R7. Every interactive element has the same behaviour layer.** State layers (hover 8%, focus 10%, pressed 10%), a 2px primary focus ring offset 2px, a 48px touch target on compact, `prefers-reduced-motion` respected.

**R8. Type scale, not sizes.** Every text uses an M3 role from `m3.css` (title, body, label). No ad-hoc font sizes.

**R9. Do not change behaviour.** Playback, scrubbing, polling, login gate and routes stay as they are. We change markup and CSS only. Where an M3 component needs a JS change, it is stated here.

**What we deliberately do not use.** Navigation rail and bottom navigation (three cameras do not justify a second navigation surface; tried and rejected by the owner). FABs (no single dominant creation action). Large top app bar collapsing behaviour (a fixed small bar is right for a viewer). `@material/web` and any CDN.

## Decisions: the player page

### App bar (one row)
| Element | M3 component | Rule |
|---|---|---|
| Brand and title | Small top app bar leading + headline (title-large) | R8 |
| Camera tabs | Primary tabs: label plus status dot, 3px primary indicator under the active label, vertically centred on the bar's centre line with the indicator on the bar's bottom edge | R2, R3 |
| "Set up" camera tab | Same tab, dashed bottom border, no indicator | R4 |
| Add camera | **Icon button `+`**, directly after the last tab, tooltip and aria-label "Add camera" | R2 |
| Theme (Auto/Light/Dark) | Icon button, cycles as today | R2 |
| Settings | Icon button; text label next to it only at expanded widths (kept from today) | R2 |
| Compact camera switch | Dropdown button (tonal) opening a modal bottom sheet; sheet items are list items with the selected one in secondary container | R5, R3 |

### Transport card (below the video)
| Element | M3 component | Rule |
|---|---|---|
| Live / Recordings | Segmented button, two options, selected = secondary container | R4, R3 |
| Speed 1x to 16x | Segmented button, five options, disabled in Live | R4 |
| Sound | Icon toggle button (tonal when on), Live only | R2, R4 |
| Previous / Next day | Icon buttons around the date | R2 |
| Date label | Text button that opens the date picker | R1 |
| Export | **The one filled button on the page** (R1), leading icon | R1 |
| Card | `surface-container` card, no border | R6 |

### Timeline
| Element | M3 component | Rule |
|---|---|---|
| Scrubber | Slider-style track: track in surface-container-highest, recorded ranges in secondary, playhead and its value label in primary (label = plain tooltip style). Canvas drawing stays; only its colours and the handle shape change. | R3, R9 |
| Time ticks | label-small on-surface-variant | R8 |
| Keyboard and touch scrubbing | unchanged | R9 |

### Events panel
| Element | M3 component | Rule |
|---|---|---|
| Panel | `surface-container` card; on compact a bottom sheet with a drag handle | R5, R6 |
| Title and count | title-medium plus a small badge | R8 |
| Refresh | Icon button | R2 |
| Type filters | Filter chips (selected = secondary container with check) | R4 |
| Event row | Two-line list item: thumbnail leading, time and type as text, per-row Export as trailing icon button (visible on hover/focus on desktop, always on touch) | R2, R4 |
| Empty / error / warning | Inline banner using error container / warn / ok containers; one banner per issue | R3 |

### Player overlay
| Element | M3 component | Rule |
|---|---|---|
| Video | native `<video>`, not restyled | R9 |
| Status message over video | Snackbar-style dark surface (inverse-surface) for info; error container for errors | R3 |
| PTZ pad | Four icon buttons on a translucent scrim surface, same state layers | R2, R7 |
| Presets | Tonal button opening a menu; menu items are list items | R1, R4 |

### Dialogs
| Element | M3 component | Rule |
|---|---|---|
| Export | Basic dialog: headline, outlined text fields (from/to), dropdown (format), linear progress, actions: text button (Close) and filled button (Export) | R1, R4 |
| Confirm dialogs (Settings) | Basic dialog with a text button and a filled (or error) button | R1 |

## Decisions: Settings, wizard, devices (B7, summarized)
Settings: a list-detail layout; the side list is a navigation list with a selected item in secondary container; panes use section headers and cards. Wizard: a linear stepper (segmented progress) and one filled button per step ("Continue"); Back is a text button. Devices: list items in a card, Allow as a tonal button, Dismiss/Remove as text buttons. Toggles are switches (R4). Destructive actions use an outlined error button inside a card, with a confirm dialog (R1, R3). Details are specified when B7 starts.

## Not decided by this page (needs your input)
1. **Add camera on compact (phone).** Icon `+` in the bar is hidden on compact today (the dropdown sheet has "Add camera"). Keep that, or show the `+` on phones too?
2. **Selected-state colour.** Segmented buttons and chips use secondary container, which in the navy palette is a light blue-grey. Alternative: primary container. Secondary container is M3's default.
3. **Theme button.** Keep the one-button cycle (Auto, Light, Dark) or move it to Settings only, leaving the bar with `+` and Settings.

## Build order once approved
B5a tokens-in-use pass on the player page (R1 to R4, R6 to R8), B5b icons (small SVG sprite, Material Symbols subset, licence file), B6 timeline and events, B7 Settings and wizard. After each step: tests, then the fake-camera screenshots at 1440, 768 and 375 px in light and dark, then your look at the real server.
