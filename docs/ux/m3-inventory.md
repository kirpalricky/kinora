# M3 inventory (B1)

Read-only audit of `kinora/web/index.html` (CSS inline 6-652), `static/app.js`, `static/setup.js`, written 2026-11-26 by haiku-researcher. Line numbers refer to the tree at branch `m3-ui` start (0.3.1). Not read line by line: setup.js 1008-1300 and 1343-1370, app.js 300-660, 745-770, 800-890 (mapped from function names and grep).

## 1. Component -> M3 component

### Player app (index.html, app.js)
| Area | Current | M3 |
|---|---|---|
| App bar | `header.pb-bar` :653, `.pb-brand/.pb-mark/.pb-name` | Small top app bar |
| Camera tabs | `#chips` tablist :655, `.pb-tab` (app.js:174), `.on`, status dot `[data-s]::after` | Scrollable primary tabs, status badge |
| "Set up" tab chip | `.pb-setup` app.js:175 | Assist chip |
| Camera switcher <600 | `#camsel.pb-camsel` :655, CSS :539-542 | Menu anchor / dropdown button |
| Camera sheet <600 | `dialog#camsheet` :718, `#camlist`, `#sheetadd`, `.pb-mi` app.js:935 | Modal bottom sheet + list items |
| Add camera | `#addcam.pb-btn.ghost` :656 | Text button |
| Theme | `#themebtn` :658, 3 SVGs `[data-i]`, `applyTheme` app.js:913, `#themestatus` | Icon button (cycles Auto/Light/Dark) |
| Settings | `#settingsbtn` :660, `.pb-settings-lbl` | Icon button (+label >=960) |
| Video | `#vwrap` :665, `video#v`, `canvas#freeze` | Keep native; not restyled |
| Video message | `#msg` aria-live :665, `.err` | Snackbar / inline banner |
| Live/Recorded | `#modeseg.pb-seg` :677 radiogroup (`#live`, `#recbtn`) | Segmented button |
| Sound | `#sound.pb-sound` :681 | Icon toggle button |
| Speed | `#speed.pb-seg` :682, `.pb-seg-btn[data-r]` | Segmented button |
| Date | `div.date` :686, `#prev/#next`, `#dlabel`, hidden `input#date` | Date picker + icon buttons |
| Export button | `#exportbtn` :687 | Filled/tonal button with icon |
| Transport card | `#ctl.pb-card` :675 | Outlined card |
| Scrubber | `#tlwrap` :692, `canvas#cv role=slider`, `#bubble`, `#head`, draw() app.js:~270 | Slider (custom canvas), tooltip-style value label |
| Camera alert | `#camstat role=alert` :690 | Warning banner |
| Status line | `#livestat.pb-status` :691 (`t-ok/warn/bad/accent`; `t-mut` has no rule) | Supporting text + dot |
| Tooltips | `[data-tip]` CSS :113-121, `setTip` | Plain tooltip |
| Events panel | `aside#evpanel` :695, `#evs`; <600 bottom sheet `#evpeek` + `setEvSheet`; 600-959 card under timeline | Card / standard bottom sheet |
| Events header | `#evhead` :697, `.ev-top`, `h3#evh`, `.pb-count#evcount`, `#evrefresh` | Card header, badge, icon button |
| Filters | `#filters` :699, `button.chip` app.js:561 | Filter chips |
| Event rows | `div.ev` app.js:649, `.evmain`, `.exbtn`, thumb `span.th` | Two-line list item, leading image, trailing icon button |
| Event states | `div.ev-state` app.js:568, `.ev-alert/.ev-warn`; skeletons `.chsk/.evsk` | Banners, empty state, custom skeleton |
| PTZ pad | `#ptz` :666 (`data-d`), overlay CSS :200-210 | Grouped icon buttons on floating surface |
| PTZ presets | `#ptzpbtn` :670, `#ptzplist role=menu` :671, `#ptzmsg` :672 | Menu button, menu, snackbar |
| Export dialog | `dialog#exp` :724, `#expstart/#expend` datetime-local, `select#expmode`, `progress#expbar`, `#expmsg`, `#explinks`, `#expcancel`, `#expgo` | Basic dialog, outlined fields, dropdown, linear progress, text + filled buttons |

### Settings and setup wizard (setup.js)
| Area | Current | M3 |
|---|---|---|
| Overlay | `section#setup` :705, `header#sutop`, `#setupback`, `#setuptitle` | Full-screen dialog, small top app bar |
| Wizard progress | `nav#sunav`, `ol.su-seg-list` setup.js:475 | Segmented linear progress |
| Footer | `div#setupfoot` :713, `navRow` setup.js:391 | Sticky action row |
| Buttons | `btn()` setup.js:366: default outlined, `.go` filled, `.ghost` text, `.bad`, `.bad-o`, `.sm` | Filled / outlined / text / error |
| Text fields | `labelled()` setup.js:370, `setErr`, `.su-hint`, `.su-err`, `.su-num-in` | Outlined text fields with supporting/error text |
| Cards | `.su-card-s` (`.su-line`, `.su-pad`, `.su-danger`), `.su-cred` rows | Outlined cards, list items |
| Chips/alerts | `.su-chip` setup.js:395, `.su-alert.{ok,warn,bad}` :400 | Assist chip, banner |
| Progress | `.su-bar-track/.su-bar-fill` (`--frac`) setup.js:942 | Linear progress |
| Disclosures | `.su-more > details` | Expansion list item |
| Sidebar | `nav.su-side` setup.js:956, `.su-row`, `.su-dot`, `.su-row-mv` | Navigation list, badge, icon buttons |
| Panes | `.su-pane`, `.su-sec`, `.su-ident` | Content column, list subheaders, large headline |
| Theme / sharing choice | `.su-seg-group` radiogroup setup.js:1370, :1460 | Segmented button |
| Toggles | `button.su-switch[role=switch]` setup.js:1219 | Switch |
| Devices | `paneDevices` :1442, `allowedBox`, `pendingBox` (Allow / Dismiss), `addressBox` + QR, `addDeviceForm` | List items in cards, tonal Allow/Dismiss |
| Confirm / QR dialogs | `dialog#suconfirm.su-dlg` :716, `openQr` | Basic dialog |
| Toast | `div#sutoast` :715, `toast()` setup.js:454 | Snackbar (+ action) |

## 2. Theme mechanism and colours
- Tokens `--c-*` on `:root` (light) index.html:8-14; aliases :15-17; dark via `@media(prefers-color-scheme:dark){:root:not([data-theme="light"])}` :30 and `:root[data-theme="dark"]` :34; light forced :38. `applyTheme` app.js:913 sets/removes `data-theme`; `localStorage.theme`; canvas redraws on change.
- Light: bg #F5F5F8, surface #FFF, text #17181F, accent #4640B8, ok #1F7A4D, warn #8A5300, bad #B3261E. Dark: bg #111218, surface #1A1B23, text #ECECF3, accent #635FDB, accent-text #A9ACFF.
- Non-colour tokens: font :18, spacing `--s-1..9` :20, radius :21, elevation `--e-1` :22, layout vars :25-28.
- Hard-coded colours: `#000`, `#222`, ptz overlay rgba blocks, dialog backdrops (`rgba(0,0,0,.45)`), event type colours in app.js:2 (`#5B8DEF`, `#EE88AA`, `#EBBF62`, fallback `#8b5cf6`). Canvas reads `--track`, `--rec`, `--c-bad`, `--mut` (app.js:279-295).

## 3. Breakpoints
600 / 720 / 960 / 1280 px (`min-width:600`, `599`, `719`, `720-959`, `960`, `960-1279`), container query `main` `@container (max-width:839px)` :528, `min-height:760` :644, `prefers-reduced-motion` :100/:266/:563, `hover:none` :573, safe-area insets, `dvh`. JS: `narrow()` = max-width 719 (setup.js:420). M3 classes needed: compact <600, medium 600-839, expanded >=840.

## 4. Tests that pin the current markup (changes need `TESTS-CHANGED`)
- `tests/test_player_logic.js`: no `style=` or inline `on*=` in index.html/app.js (:82-83); no duplicate ids (:166); exact root media strings for 600/960/960-1279 (:144); `#vwrap #ptz` top/right `var(--s-4)` (:145-146); `#themebtn`, `#settingsbtn`, `class="pb-btn pb-settings"`, `aria-label="Settings"` (:163-165); asset versions `app.js?v=70`, `setup.js?v=18` (:170, will change with `?v=<VERSION>` in B3); `applyTheme` stub behaviour (:172-194); thumbnail classes `th-ok/off/fail/load/wait`.
- `tests/test_setup_logic.js`: CSS string pins at :225-246 (`--track`, `--rec:#5C5F75`, `--c-line-strong`, `--c-text-2/3` values, `#setup :where(input…)`, `.su-switch::before` hit area, `.su-dlg::backdrop`, `.su-toast` border and `bottom:96px`, `.pb-seg-btn.on/[aria-checked]`, `.su-row[aria-current="page"]`, `--c-accent-soft:#E8E8F8`, `#evhead` sticky); the setup CSS block must contain no hex/rgba except the backdrop; setup.js role/aria strings (:247-272); no inline style/handlers in index.html+setup.js.
- `tests/test_setup_dom.js`: `data-focus` keys, `#suconfirm`, `dataset.qr`, `.themeGroup` aria-checked, `su-card-s/su-more-d/su-icon-only`.
- `tests/package_smoke.sh`: required files, `/static/app.js`, `/static/hls.min.js`.
- `.verify/checks.json:60`: exercise changed UI at desktop and narrow, light and dark; `node --check app.js`; no console errors. No selectors.

**Consequence:** the colour-token tests (`--c-*` literal values, `#E8E8F8` etc.) will fail when the palette changes. Those lines are an intended behaviour change and must be listed under `TESTS-CHANGED`. Keep the existing `--c-*` names as aliases of the new M3 roles where it avoids churn.

## 5. Inline styles and CSS built in JS
- No `style=` attributes in HTML (enforced). CSSOM writes: app.js:139/141, :300 (bubble/head left), :562, :657 (dot background), :732/752/758, :791, :805; setup.js:946, :1673, :1679 (`--frac`).
- JS builds markup via innerHTML: chips app.js:174, filters :561, skeletons :554/:673, event rows :653-656, links :759-762. Icons: `SU_ICONS` + `icon()` setup.js:316-365 (createElementNS paths), PTZ mask SVG data-URI index.html:224-225.
- Classes/ids set by JS with no CSS rule: `.su-invalid`, `.su-open-reason`, `#su-rows`, `.su-acct-*`, `.t-mut`, `.sheet`, `.su-foot`.
