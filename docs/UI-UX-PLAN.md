# Pixel QSO distribution UI/UX plan

Date: 2026-10-09. Scope: desktop application; coordinate shared terminology with mobile control. This is a proposal, not an implemented redesign.

Beta 1 follow-up: the first-run Getting started guide and Operate → Configure beacon shortcut were removed. Backend switching is in Station settings → Audio & Radio. See the latest implementation status and validation boundary in [NEXT.md](../NEXT.md).

## Product direction

Make Pixel QSO approachable to a radio operator who understands callsigns, audio devices, and CQ but does not understand modem framing, KISS ports, or decoder internals. The primary journey is: set up station → listen → call or reply → exchange cards → revisit/share a contact.

Keep the native Qt application. Retain the distinctive pixel artwork and restrained teal accent; give the surrounding interface the spacing, hierarchy, and consistency of a finished desktop product.

## Audit basis

Reviewed `app.py` screen construction, settings, backend visibility and state handling, `README.md`, and the checked-in QSO/editor screenshots. At the initial audit those screenshots were older than the code. On 2026-10-09, the README's Operate and card-editor screenshots were refreshed from the current PySide UI in offscreen Qt; the Operate image shows a fresh disconnected profile, and the editor image shows the included 73 Sunset starter card. These captures establish rendered layout only; no live application session or radio path was exercised.

Current strengths:

- QSO, My Cards, and QSO Log already provide a useful navigation foundation.
- Automatic exchanges, direct replies, quick drawing, starter cards, PNG sharing, and progressive reception already exist.
- Data2G already has a recommended mode shortlist, and experimental modems already require opt-in.
- Persistent PTT fault handling and verified receive promotion provide important behavior to preserve.

Primary friction:

- Operating controls mix user intent (call, reply, send) with exchange roles, stage assignment, backend selection, and modem configuration.
- Several transmission actions compete for attention; the user must infer which action fits the current state.
- Station settings combines identity, audio, connection internals, experimental options, and remote control in a long form.
- Repeated bordered groups, dense forms, and many similarly weighted buttons compete with the cards themselves.
- The editor gives stamp management and technical format controls substantial space; the library exposes six toolbar actions and separate competing galleries.
- Documentation and screenshots contain outdated controls. Refresh both as part of distribution preparation.

## Navigation and operating screen

Use three destinations: **Operate**, **My Cards**, **Contacts**. Put Settings and Help in a consistent application header, available from every destination.

Operating layout:

```text
Pixel QSO        Operate   My Cards   Contacts        Settings
Station: W1ABC · FN42     Connection: Connected     RX: Listening
──────────────────────────────────────────────────────────────
Your card / contact             Current contact / incoming card
[card preview]                  W2XYZ · receiving card…
[Choose card] [Edit]             [large image + honest progress]
Mode: Everyday HF               [Reply to W2XYZ] when applicable
Estimated airtime: …
[Call CQ]                       Recent cards with call/time/state
[Send card ▾]                   [thumbnail] [thumbnail] …
Contact card set…               Activity details ▸
──────────────────────────────────────────────────────────────
Calling CQ · next call in 5 s                         [Stop]
```

This is an information hierarchy, not a pixel specification. Use a resizable splitter; adapt to a narrower window without pushing the active action or Stop off screen.

- Default to listening; initial setup and connection status explain what is missing.
- **Call CQ** starts the existing automatic sequence. **Reply to [callsign]** starts the responder sequence from a selected eligible CQ. No role selection is required for either.
- Keep **Send card** available as a secondary everyday action. Put manual stage transmission under Advanced. Beacon is a discoverable alternate send action with its own interval panel.
- Display the current peer and stage as a compact timeline: Calling → Exchanging → RR73 → Complete. Keep protocol labels available with short explanations.
- Contact-card assignments move to **Contact card set…** with usable starter assignments and previews of each stage.
- Use one prominent contextual primary action. During an active sequence, show its state and a persistent **Stop** that cancels current transmission and queued automatic/repeat work through existing cancellation mechanisms. If stopping is pending, say so until acknowledged.
- Keep CQ intervals in seconds and beacon intervals in minutes. Beacon starts with an immediate send at the current frequency. Show active repetition and next-send status even when advanced panels are collapsed.
- Show frequency in readable MHz only when known, including whether it is reported or configured. Never imply that a displayed value is a live rig reading or that Data2G can be tuned here without that capability.

## Progressive disclosure

Provide a persistent **View → Advanced controls** toggle, off for new installations. Add small local **Advanced…** disclosures in Settings/editor where useful; avoid a second, independent global mode. Preserve existing settings and mode IDs when changing visibility.

| Area | Everyday view | Advanced view |
| --- | --- | --- |
| Operation | Listen status, Call CQ, Reply, Send card, card choice, airtime | Manual stages, full modem catalog, local receive-all, copy count, timing calibration |
| Mode | Available Everyday HF / Narrow HF / Faster choices with short descriptions | Exact modem ID, bandwidth, capacity, coding and compatibility details |
| Station | Callsign, grid, audio input/output, radio connection status | rigctld address/port, backend selection, remote host and command/KISS ports |
| Editor | Drawing, text/stamps, colors, undo/redo, save | Exact dimensions/color count, WAV export, detailed format constraints |
| Receive | Incoming image, sender when known, progress, verification state | Waterfall when supported, decoder/frame details, file decoding, logs |
| Sharing/control | Copy image, Export image | JSON export, mobile-control service settings and diagnostics |

Experimental modems remain a separate explicit opt-in within Advanced. Opening Advanced alone must not enable them or change the selected backend. An active custom/experimental setting remains summarized visibly with an Edit link after collapsing Advanced. Unavailable modes need an accessible inline reason, not only a hover tooltip. If no recommended mode is available, explain that and link to available compatible modes; never silently substitute an incompatible mode.

## Setup and settings

First launch offers a short, resumable setup flow:

1. **Your station:** callsign and optional grid, with inline validation and examples.
2. **Connect your station:** recommended managed Data2G connection; choose input/output and necessary radio settings. Offer “Use an existing remote host” as a secondary branch.
3. **Check reception:** connection result and actual receive activity/levels where supported. Explicitly distinguish host connectivity from audio/PTT readiness. Do not invent a remote level meter when the host supplies no telemetry.
4. **Choose a starter card:** preview, personalize, then open Operate. Allow receiving/exploring before completing transmit configuration.

Any transmit check is an explicitly labeled user action; setup must never key the radio automatically. Preserve the current fault recovery acknowledgment.

Replace the single long settings form with **Station**, **Audio & Radio**, **Appearance**, and **Advanced** pages. Show only fields applicable to the current connection/backend. Basic radio setup must remain reachable without requiring knowledge of internal backend terms.

Use explicit Apply/Cancel for connection changes. Identify changes requiring reconnection and offer **Apply and reconnect** with a brief interruption explanation. Do not reconnect during an active transmission. Keep identity editing constraints understandable, and preserve pending edits when validation fails. Define immediate-save preferences separately and label their behavior consistently.

## Reception, status, and errors

- Separate connection state, receiver state, and exchange state. A connected transport is not proof of a working RF path.
- Show **Signal detected**, **Receiving card**, **Partial card — waiting for more**, and **Received** only when corresponding evidence exists.
- Preserve early/provisional pixels. Label them clearly and do not treat image coverage as verified completion. Data2G cannot supply within-burst pixels that the host has not delivered.
- Distinguish **Sent** (local transmission completed) from **Contact complete** (verified protocol exchange). Do not imply remote receipt from ACKMODE alone.
- Keep significant failures visible until resolved. Use a short explanation plus one useful action: **Choose audio device**, **Reconnect**, **Open radio settings**, or **Review PTT fault**.
- Put technical detail behind a disclosure with a copyable diagnostic report. Redact secrets and private connection data before sharing; do not upload automatically.
- Use brief notifications for successful saves/copies. Reserve modal dialogs for decisions, potential data loss, and required fault recovery.
- Empty screens should explain the next step: choose a starter card, connect to listen, or call CQ to start a contact.

## Card editor, library, and contacts

Editor:

- Keep a large canvas and a compact labeled toolbar: Draw, Erase, Fill, Pick, Text, Stamps. Place Undo/Redo together.
- Show stamp movement/resizing/deletion only when relevant to a selected stamp. Add direct manipulation handles; keep precise size entry in Advanced.
- Default format follows actual selected-mode compatibility; explain constraints before destructive resizing or palette reduction and keep an undo path.
- Show station identity by default, with explicit per-card overrides. Preserve existing card metadata on edit.
- Provide a clear card title, saved/unsaved state, and consistent Save/Close semantics. Guard against losing unsaved work.
- In quick-reply context, show the intended recipient and label the action **Send reply to [callsign]**. In library context, use **Save card**.

Library:

- One searchable gallery with filters such as My cards, Sent, and Starter cards.
- Primary **Create card** action; selected-card details expose Edit and Copy image, with Duplicate/Export/Delete under a labeled More menu.
- Double-click opens editing. Show names, dimensions, and intended stage where useful, without turning every tile into a technical report.
- Keep JSON import/export available for editable interchange; make PNG sharing the obvious common path.

Contacts:

- Consistently use **Contacts** in navigation and page title.
- Search by callsign/grid, sort by date, and show completion/partial status explicitly.
- Open a contact to a chronological sent/received card history with clear direction and timestamps.
- Keep Copy image and Export readily available; preserve existing contact-sheet behavior.

## Visual and accessibility system

- Centralize theme tokens and reusable widgets instead of adding more isolated styles to `app.py`.
- Use an 8 px spacing rhythm, 16–24 px panel padding, restrained borders, and consistent 6–8 px corner radii.
- Define a small type scale using native system fonts and scalable sizes: page title, section title, body, supporting text. Avoid tiny metadata and fixed text geometry.
- Use neutral surfaces, a teal primary accent, and distinct warning/error colors. Reserve bright saturated color mainly for card artwork.
- Use a consistent icon set with text labels on important actions; avoid relying on emoji for cross-platform application controls.
- Provide visible keyboard focus, logical tab order, accessible names, and status text alongside every color indicator. Do not make hover the only route to explanations.
- Target 4.5:1 normal-text contrast and 3:1 large-text/control contrast; verify measured values during implementation.
- Support system/light/dark appearance using semantic tokens. Start with one fully polished theme, then validate the others before declaring them supported.
- Design for 1024×768 and 1366×768 logical desktop sizes, larger windows, and 100–200% display scaling. Scroll secondary content; keep actions reachable. Preserve crisp nearest-neighbor scaling for pixel art.

## Implementation sequence and acceptance gates

| Phase | Work | Acceptance gate |
| --- | --- | --- |
| 1 — Operating flow | Intent-based actions, current-peer context, persistent Stop, Advanced disclosure, honest status | A user can listen, call CQ, reply, send a standalone card, and stop repetition without touching modem internals; hidden settings remain preserved |
| 2 — Setup and recovery | First-run flow, categorized settings, device errors, reconnect flow | Fresh installation reaches a usable receiving screen; missing devices, unavailable modes, remote disconnects and PTT faults have visible next steps |
| 3 — Visual system | Theme tokens, typography, shared controls, adaptive layout, keyboard navigation | Primary screens and dialogs remain legible and actionable at target sizes/scales; no clipped actions or color-only status |
| 4 — Cards and contacts | Contextual editor, unified gallery, clear save states, contact detail/share | Create → edit → save → use → share is discoverable; cancel/close behavior preserves user work |
| 5 — Distribution finish | Help/About, accurate screenshots/docs, packaged first-run/recovery review | Supported packages complete the same user journeys; known platform and hardware limitations are documented |

Introduce theme tokens and reusable state/action components during phase 1 so the operating redesign does not require a second structural rewrite in phase 3. Prefer incremental Qt widget extraction over replacing the UI framework. Keep transfer/assembly/backend semantics in their existing services. Present the UI from shared capability and operation state so multiple buttons cannot disagree about whether transmitting is allowed.

### Implementation checkpoint

Phase 1 has started. The main destinations are labeled **Operate**, **My Cards**, and **Contacts**. The operating header identifies the configured callsign/grid and current connection; the setup button reads **Connect station** until its selected radio-control path is connected. Active automatic exchanges show the peer, current stage, and a four-step contact timeline. A saved **View → Advanced controls** preference hides manual stage assignment, backend/exchange selection, timing calibration, receive-all, and supported spectrum visuals by default without changing saved values. **Send card** is the everyday manual action, with a clear disabled appearance and an inline readiness reason when transmission is unavailable; beacon setup remains available from **Operate → Configure beacon…**. The connection summary names Data2G when selected and states that audio/PTT readiness is unverified; PTT faults remain explicit. The receive indicator now distinguishes startup, connection, waiting for frames, and active listening, so a host connection is not presented as proof of active reception. Station settings are grouped into **Station**, **Audio & Radio**, and **Advanced** pages, with scrollable content for device and host settings. A persistent **Settings → Station settings…** action with **Ctrl+,**, a reopenable **Settings → Getting started…** guide, and **Help → About Pixel QSO** are available from every destination. The guide is offered on first normal launch and walks through station details, connection, reception, and starter cards without blocking later use. Station identity fields now provide inline call sign/grid feedback and examples. My Cards offers previewable starter cards and moves duplicate/delete actions into **More**; the desktop bundle includes the starter assets. Its fresh-profile view presents centered **Create a card** and **Use a starter card…** actions; the toolbar actions hide until cards exist, and selection actions appear only after a card is selected. Empty contact states also give a next step. Existing UI tests cover these flows along with backend-specific visibility, advanced control persistence, connection summary, and timeline states.

The operating layout still needs a fuller contextual action hierarchy and a broader state review. The first-run setup hub is an initial setup path; connection edits still need explicit Apply/Cancel and safe reconnect handling. The application shell, receiver indicator, and Data2G connection/PTT status now use semantic dark-theme tokens from `ui_theme.py`; the shared stylesheet also outlines focus on common controls and scales its typography with the Appearance preference. Station settings now has an Appearance page with a staged, persistent Standard/Larger (115%) interface text-size choice that also scales the application font from a stable unscaled baseline and has a sample preview before Apply. Many local inline styles remain, custom widget focus styles and keyboard traversal have not been reviewed, and supported light/system color themes are still unavailable. Verify Apply/Cancel and layout at the larger setting. In the card editor, stamp shelf actions are contextual to a selected saved stamp, placed-stamp resize/remove controls are contextual to a selected placement, and the window title marks unsaved card identity/canvas changes. Contact details identify direction and stage, display `logged_at` on new events, and now offer direct Copy PNG and Export actions; shared contact sheets include direction/stage/time, and older records without an event time state that it was not recorded. The card library now presents My, Sent, and Starter cards in one searchable gallery with category filters and category-appropriate selection actions. This implementation has not been tested or visually reviewed; confirm filtering, duplicate entries across categories, selection actions, and layout before treating the gallery acceptance gate as complete. Editor hierarchy, contact-share verification, distribution, and operator review also remain open. This checkpoint records progress and does not change the acceptance gates below.

Suggested UI boundaries: application shell, operating page, station setup/settings, receive panel, card editor, library, contacts, theme tokens. Preserve settings keys, card files, contact history, explicit mode selections, and safety interlocks during migration. Advanced visibility is a presentation preference, never a configuration reset.

Future validation should cover state transitions and cancellation, settings persistence/migration, provisional versus verified receive behavior, fresh-profile packaged startup including the first-launch guide, and real desktop visual/keyboard checks. Use controlled fixtures for repeatable UI states; audio/CAT/PTT and two-station RF checks remain separate evidence. Focused offscreen UI tests for the current operating, setup, navigation, and library changes pass; the setup guide was reviewed at 1024×768. Hardware and packaged-startup checks remain outstanding.

Conduct a short task-based review with semi-technical operators: configure station, identify whether reception is active, call CQ, reply, stop a repeat, create a card, and share a contact. Record task success, assistance required, and mistaken expectations. Distribution readiness requires both correct behavior and understandable feedback.

Recommended first delivery: the Operate screen and shared status/action model, including Advanced disclosure. This produces the largest daily usability improvement and establishes the structure for setup and visual polish.
