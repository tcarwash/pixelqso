# Bringing the PixelChat style to PixelQSO

Design direction · 2026-10-16 · First visual pass implemented on `pixelchat-inspired-ui`

The reference is the initial PixelChat prototype at `/home/tyler/dev/pixelchat/mobile/`,
commit `7fcdda3`, particularly `style.css`, `index.html`, and `app.js`. The user liked
this visual style and requested a guide for recreating it in the native PixelQSO app.
This document captures the design independently of the running web preview.

## The feel to preserve

A small pixel-art collection should feel like something you want to hold and share.
The UI supplies a dark, slightly violet backdrop; the artwork supplies the personality.
Lavender card frames and a bright lime primary action make the important objects obvious.
Large, warm headings and generous space make each screen understandable at a glance.

Five recognizable ingredients:

1. Almost-black violet surfaces, with subtle tonal separation rather than boxes everywhere.
2. Cream-white typography, muted lavender-gray supporting text, short headings.
3. A substantial lavender collectible-card frame around crisp, unfiltered pixel artwork.
4. One lime primary action per current task, with quiet secondary buttons.
5. Simple navigation with a clear selected state and a short, visible activity/status message.

PixelChat is a sound-sharing mobile app. PixelQSO is a desktop radio application. Adopt the
visual language while preserving the operator's radio controls, identity, and exchange model.
This is not a proposal to replace the native app with a web view or change its modems.

## Color recipe

These values come from the actual PixelChat CSS. Qt names below are proposed mappings
into the existing `ui_theme.py` semantic token system, not claims about current values.

| Role | Reference value | Suggested Qt mapping/use |
| --- | --- | --- |
| Window backdrop | `#101118` | `window` |
| Subtle background glow | `#252435` fading into backdrop | Optional painted background |
| Navigation surface | `#191a24` | `surface_tab`, header/navigation |
| Raised panel | `#252532` | `surface_raised` |
| Secondary button | `#292a37` | Secondary action background |
| Collection tile | `#292937` | Gallery item surface |
| Main text | `#f5f1e9` | `text`, `text_bright` |
| Supporting text | `#aaa8b9` | `text_muted` |
| Small section label | `#aba9bb` | Eyebrow/section label |
| Quiet hint | `#92909f` | Nonessential hints only |
| Borders | `#383744` / `#41414f` | Panel / control edges |
| Primary action | `#c5ff88` | `accent` |
| Primary action text | `#20251c` | `text_primary` |
| Keyboard focus | `#c6ff8a` | `focus_ring` |
| Collectible frame | `#bda5f8` | `card_frame` |
| Text on frame | `#252039` | Card title and subtitle |
| Artwork backdrop | `#151522` | `card_art_background` |
| Selected navigation | Lime text on a very faint lime tint | Selected tab/rail state |

Keep warning, error, and positive status tokens semantically distinct. Lime is the normal
brand/action color, so it cannot by itself mean a successful or verified reception. Keep
words and icons alongside state colors. Measure contrast for actual text/background pairs,
especially small hints, disabled controls, and text placed on card frames.

## Typography, rhythm, and shape

Use the platform sans-serif font through `QFont`; no downloaded font is required. Use
weights and size to create hierarchy, with monospace reserved for technical readouts.

| Element | PixelChat reference | Desktop adaptation |
| --- | --- | --- |
| Main heading | 38 px, heavy, tight tracking | Start at 28–32 logical px; 38 for an empty/welcome screen |
| Brand | 25 px, bold | 22–25 logical px |
| Body/help | 14 px, relaxed line height | 14–15 logical px; respect system scaling |
| Section eyebrow | 10 px, uppercase, 2 px tracking | 11–12 logical px; short optional labels |
| Card metadata | 11–12 px, bold | 12–14 logical px |
| Screen margins | 24 px | 24 logical px |
| Card/panel padding | 16 px | 16–20 logical px |
| Gallery gap | 16 px | 16 logical px |
| Primary button | 52 px high, 14 px radius | 44–52 logical px high, 12–14 radius |
| Feature card | 20 px radius | 18–20 logical px |
| Secondary tile | 14 px radius | 12–14 logical px |
| Fields | 10 px radius | 8–10 logical px |

Extend `SPACING`, `RADIUS`, and `TYPE_SIZE` centrally. Avoid scattering literal values through
individual dialogs. CSS pixels are a design reference: validate the result with Qt's font
metrics and high-DPI scaling rather than assuming identical physical size.

The web feature card has a gentle -2° tilt and a soft shadow. Treat those as optional delight.
Use straight cards in galleries and the editor. If adding a tilted hero, paint the card in a
custom widget; do not rotate interactive child controls or distort the pixel image.

## Adapt the screens to PixelQSO

Keep the current **Operate · My Cards · Contacts** structure for the first pass. The existing
editor remains accessible through Edit/Create. PixelChat's Send/Receive/Canvas/Collection
labels are useful task references, not a requirement to split PixelQSO's live operating screen.

### Operate

Use a compact header with PixelQSO branding, navigation, and Settings. Immediately below it,
retain readable station identity, frequency where available, connection state, and RX/TX state.
Use the middle of the window for two substantial cards: selected outgoing art and incoming
art/current contact. Each gets a short label and only the metadata relevant to its role.

```text
PixelQSO       Operate   My Cards   Contacts                  Settings
Station / grid          Connection state          Frequency / RX or TX

YOUR CARD                                  INCOMING CARD
[lavender frame + crisp art]                [large art / receive state]
[Choose card] [Edit]                        Callsign · coverage / verification

                   [one contextual primary action]
                   Short, truthful status message

[compact waterfall / useful receive controls]
[Advanced controls, collapsed when not needed]
```

Make the relevant call/send/reply action prominent according to the existing exchange state.
Keep Stop reachable and clearly labeled during transmission or automatic exchange. The
waterfall, tuning, audio status, and PTT failure information must remain usable; do not bury
operational controls merely to imitate a mobile hero screen.

### My Cards

Recreate the collection tiles: rounded dark surface, generous padding, square pixel preview,
short creator/callsign title, and one muted metadata line. Use a responsive desktop grid,
not a fixed two-column phone layout. A selected tile gains a restrained lime border/tint.

Selecting a card changes selection only. Edit, Export, and Send remain explicit actions.
Let artwork dominate; put secondary card details in a detail pane or context menu.

### Canvas/editor

Give the canvas the largest continuous area. Use the same dark surfaces and rounded tools,
with neighboring palette swatches, a visible selected color, and a clearly grouped tool strip.
Carry over the calm spacing around creator/card metadata and Save.

Retain the existing drawing, stamps, layers, selections, undo/redo, animation authoring, and
keyboard shortcuts. PixelChat's smaller toolset is not a reason to remove desktop features.
Keep actual pixel colors unchanged: recolor the surrounding chrome, not the card palette.

### Contacts and settings

Use collection-style previews in contact decks, with readable contact information beside them.
Use the same typography and spacing in Station Settings, but keep real form labels and the
existing Apply/Cancel semantics. Save playful copy for empty states; operational feedback
should remain direct: “Listening”, “Receiving card”, “Sending”, “Unverified”, “Verified”.

## Where to implement it in the existing code

| Existing location | Proposed work |
| --- | --- |
| `ui_theme.py`: `COLORS`, `SPACING`, `RADIUS`, `TYPE_SIZE`, `build_app_stylesheet()` | Central palette, typography, controls, focus, hover, checked, disabled states |
| `app.py`: `MainWindow`, `make_exchange_tab()` | Header hierarchy, outgoing/incoming card balance, primary action grouping |
| `app.py`: `make_library_tab()`, `make_qso_log_tab()` | Gallery spacing, selection styling, metadata hierarchy |
| `app.py`: `make_card_tab()`, `EditorDialog`, `CardCanvas` | Editor chrome, palette/tool grouping; preserve canvas interaction behavior |
| `card_views.py`: `card_art()`, `ArtworkView`, `DeckDialog` | Shared frame, title/subtitle colors, art padding, deck presentation |
| `docs/UI-UX-PLAN.md`, `NEXT.md` | Reconcile accepted design direction; record implementation and validation checkpoints |

The older UI/UX plan calls for a restrained teal accent. This guide proposes a lavender/lime
alternative based on the user's PixelChat preference. Reconcile that wording when implementing
an agreed visual pass; do not leave contradictory descriptions of the final design.

Use Qt stylesheets for ordinary fills, borders, radii, padding, and button states. Reuse the
existing `primaryAction` object name and semantic styling helpers. Use `QPainter` for custom
card frames, rounded image clipping, gradients, and optional decoration. Qt stylesheets are
not browser CSS: do not copy `transform`, `backdrop-filter`, `box-shadow`, or animation rules
verbatim. A flat opaque navigation bar is an acceptable translation of the web blur effect.

Render pixel art with nearest-neighbor scaling and preserve its aspect ratio. Avoid smooth
pixmap transforms for the art itself. Keep decoration separate from export content unless a
framed export is explicitly requested. For optional pulse feedback, use a Qt timer/property
animation and provide a static alternative; animation is not evidence that decoding is working.

## Implementation sequence

1. **Theme and shared controls.** Update central tokens and button/tab/input styles. Render
   the current screens without rearranging behavior. Inspect primary, hover, checked, focus,
   disabled, warning, and error states.
2. **Card presentation.** Restyle shared card rendering and collection tiles. Verify palette
   fidelity, nearest-neighbor pixels, titles, selection, animated previews, and exports.
3. **Layout hierarchy.** Improve Operate spacing, contextual actions, and editor/gallery
   proportions. Preserve existing signal connections and explicit send behavior.
4. **Consistency pass.** Bring settings, contacts, menus, dialogs, empty states, and tooltips
   into the same system. Refresh screenshots and document the completed checkpoint.

A useful first review would show Operate, My Cards, and the editor using the new theme before
committing to a navigation redesign. Keep the first change reversible and focused on visuals.

## Acceptance checks

- Screens look like the PixelChat family: violet-black backdrop, lavender cards, lime action,
  warm readable headings, and space around art.
- Test representative compact and large windows plus 100%, 150%, and 200% display scaling.
  Long callsigns, translated labels, and larger fonts must not clip primary controls.
- Keyboard navigation and visible focus work across tabs, palette, forms, and card selection.
- Choosing a card never transmits. Stop remains accessible; faults and disconnections are visible.
- Partial/unverified reception remains distinct from a fully verified card. Local playback
  completion or Data2G ACKMODE must not be presented as proof of remote receipt.
- Radio/audio backend ownership, CAT/PTT, automatic exchange, settings transactions, decoder
  behavior, and saved card formats stay intact during the styling pass.
- Use focused existing UI tests for affected interactions and inspect actual Qt renders.
  Report offscreen/render verification separately from live audio/radio validation.

This file describes the full design direction. The initial theme/shared-card pass is now
implemented and rendered; the broader layout redesign remains proposed. See `../NEXT.md`
for exact validation results and remaining work. The active PixelQSO app was not restarted.

## Implemented workspace follow-up

The navigation rail, Operate outgoing preview, collection hierarchy, editor studio header,
activity disclosure, and persistent native splitters are implemented. The user explicitly
retained the four-stage radio workflow: CQ → response → signal report → 73 goodbye.
All four explicit send actions and their card assignments remain visible together. Numbered
selectors and card choices never transmit. Stage actions and Stop remain outside the outgoing
scroll area. Column minimum widths follow content, including larger interface text. Refer to
`NEXT.md` for current evidence, exclusions, and remaining live validation.
