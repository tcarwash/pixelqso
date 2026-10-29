# PixelQSO Gamification and RF Game Integration

Status: design proposal (2026-10-29). No protocol or UI change is implied by this document.

## Vision

PixelQSO should make exchanging cards over actual radio feel like collecting, completing sets, and building relationships with stations. The achievement is *successful delivery*, not merely pressing Transmit. A separate game, provisionally **Space Traders**, can use the same transport-neutral verified-transfer events to turn HF propagation, limited throughput, and store-and-forward relaying into strategy.

Keep the core PixelQSO experience useful and pleasant without points, accounts, leaderboards, or an Internet connection. Game features are optional and must never compromise QSO or card interoperability.

## Existing foundations and boundaries

PixelQSO already tracks card identity, fragments, assembly, checked payloads, QSO stages, reception, and locally stored cards/logs. Data2G host/KISS and experimental modems are transport options. Modem-level frame checks and partial previews are not by themselves evidence of a complete application delivery.

Proposed new components:
1. A normalized, local **transfer event journal** emitted by PixelQSO once a whole card or application object is validated.
2. A **receipt exchange** for confirmed two-way delivery, when supported.
3. An optional **achievements/contracts** evaluator consuming journal events.
4. A narrow **application-envelope API** that games such as Space Traders can use without importing PixelQSO UI or modem internals.

Avoid changing existing on-air card formats in the first milestone. Events can initially be local-only, with receipt negotiation added compatibly later.

## Levels of evidence

Be explicit about what the application knows:

| Level | Evidence | Safe interpretation |
| --- | --- | --- |
| 0: attempt | Local transmit request | An attempt, not a success |
| 1: receiver decode | Whole payload integrity/identity verified by receiver | Receiver obtained a valid object |
| 2: confirmed delivery | Sender receives a matching receiver-generated application receipt | Sender has evidence of acknowledged delivery |
| 3: signed receipt | Level 2 receipt binds payload hash, sender/receiver, nonce, and session with verified signature | Cryptographic attribution to a key, **not** independent proof of RF transport |
| 4: witnessed/relayed | Independent participating station supplies a separate receipt | Stronger social evidence, still not tamper-proof RF proof |

Offline receive-only users can earn local collector progress at level 1. Bilateral/competitive contracts should use level 2 or higher. Preserve a clear distinction between 'received', 'acknowledged', and 'verified signature' in UI.

A suggested event record (illustrative, versioned; not yet an implemented schema):

```json
{
  "schema": "pixelqso.transfer.v1",
  "event_id": "random-unique-id",
  "object_id": "content-addressed-or-existing-card-id",
  "object_sha256": "hex-digest",
  "kind": "card",
  "direction": "receive",
  "local_callsign": "AG7SU",
  "peer_callsign": "EXAMPLE",
  "session_id": "opaque-session-id",
  "completed_at_utc": "2026-10-29T20:00:00Z",
  "transport": "data2g",
  "mode_id": "advertised-mode-id",
  "bytes_verified": 512,
  "evidence": "receiver_decode",
  "receipt_id": null,
  "signature_status": "not_checked"
}
```

Never assume callsign, geographic range, band, mode, SNR, or RF path is authenticated because it appears in a local log. Mark operator-entered and receiver-measured metadata separately. Account for clock skew and offline peers.

## PixelQSO collection loop

**Create -> transmit -> receive -> collect -> curate -> exchange again.**

- **Card decks:** Session, formal exchange, cards from a given operator, lifetime collection, custom albums, and themed sets. Preserve both individual card instances and shared artwork identity so editions remain distinguishable.
- **Card provenance:** Show sender, date, session, receive state, and optional signature; retain original received bytes/hash. Give cards a premium trading-card presentation, without pretending rarity is cryptographically guaranteed.
- **Milestones:** First checked card, first completed formal exchange, first new correspondent, unique counterpart count, completed set, successful acknowledged exchange, optional relay contribution.
- **Series and sets:** Voluntary creator-defined collections, e.g. four 'Chip' series cards or seasonal themed decks. Set completeness is based on distinct canonical card/edition identifiers.
- **Personal station history:** A collection map and journal can show user-supplied/observed bands and contacts; never claim geographic distance or RF conditions without suitable evidence.
- **Opt-in sharing:** Export album/achievement bundles and signed attestations, but offline collection remains first-class.

Default to generous cosmetic achievements and narrative discovery, not a grind. Do not award unlimited XP per byte, per repeated frame, or per retransmission. Avoid pay-to-win and real-money value.

## Achievement and contract rules

Each contract specifies an issuer/namespace, version, objective, time window if any, unique nonce, allowed object types, evidence threshold, reward, expiry, replay policy, and deterministic evaluator version.

Examples:
- **First Contact:** receive a checked card from a previously unseen callsign (local collector achievement).
- **Handshake Complete:** both stations exchange distinct cards and matching acknowledged receipts in a defined session.
- **Collection Quest:** complete a published four-card themed series with distinct canonical identities.
- **Reliable Courier:** deliver one signed game parcel with a receipt from its designated destination, potentially via hops.
- **Three Ports:** deliver different mission manifests to three distinct counterparties; cap repetitive contacts per period.

Deduplicate by event ID, object hash, contract nonce and receiver identity as appropriate. Bound rewards to useful objectives, enforce cooldowns/caps, and treat callsign diversity as soft evidence only. No leaderboard should imply fraud-proof verification.

## Receipts and transport

For a game payload, use a versioned envelope containing application ID, protocol version, sender, intended destination, immutable message ID, creation/expiry, content hash, sequence/fragment details when needed, and optional signed metadata. Transport remains free to fragment/retry.

Receiver response: `ACK(message_id, content_hash, recipient, receiver_nonce, result, optional_signature)`. A matching ACK acknowledges receipt of bytes; application-level acceptance (e.g. a trade commit) must be a separate explicit result. Use canonical serialized bytes for hashing/signing. Store receipt and journal idempotently.

Do not force a continuous session. Store-and-forward queues, delayed ACKs, duplicate packets, out-of-order arrivals, retransmission, and peers that never return are normal conditions. Define bounded TTL, expiration, cancellation, and conflict-resolution policy per application.

Separate:
- **Transfer succeeded** (payload verified),
- **Delivery confirmed** (ACK received),
- **Action accepted** (game rules applied),
- **Finalized** (authoritative game state advanced).

## Space Traders: early game concept

### Pitch

A lightweight, persistent, turn-friendly space-economy game for amateur-radio data links. Every operator owns a starport. Players discover systems, buy cargo, manufacture goods, take contracts, dispatch ships, and relay sealed manifests across a real radio network. The world is fictional; **actual propagation constrains how quickly commands, discoveries, and cargo documentation reach other players**.

Prefer player-owned stations, with optional NPC markets and a local single-player/simulation mode. An Internet connection is never mandatory to play.

### Core loop

1. **Inspect market:** local cached prices, posted buy/sell offers, and known routes, all timestamped.
2. **Plan:** select cargo, risk, deadline, destination, and route through known relay stations.
3. **Commit:** create a small signed order or cargo manifest; reserve inventory locally.
4. **Transmit:** deliver directly or through consenting relays, awaiting eventual receipts.
5. **Settle:** destination/game authority validates contract and inventory transition, then issues a settlement receipt.
6. **Progress:** earn in-game credits, upgrade ships/starport, reveal sectors, improve reputation, collect narrative artifacts.

RF should carry compact *orders and proofs*, not huge continuous simulation state. A 100-byte trade with real strategic consequence is better than a 100-kB grind.

### World and economy

- **Starports:** Each station operates a port within an assigned fictional sector. The callsign identifies operator context, but is not necessarily the persistent game-account identity.
- **Goods:** Food, ore, fuel, machinery, medicine, rare artifacts. Different production/consumption profiles create complementary local markets.
- **Dynamic prices:** Bounded stock-based supply/demand changes per epoch; deterministic enough for replay and offline simulation. Markets advertise price, quantity, revision, and expiry.
- **Ships:** Individual hulls with cargo capacity, range, fuel requirement, travel time, condition, and upgrades. First version can abstract ship travel into ETA and state transitions.
- **Contracts:** NPC and player-issued shipping jobs with destination, amount, deadline, nonce, payout, and validity.
- **Relays:** Third-party stations carry signed opaque game envelopes and receive modest in-game courier credit on destination acknowledgment. Relays do not acquire cargo ownership by handling packets.
- **Exploration:** Discoveries are small information packets, collectible starmaps or artifacts; exploration rewards unique validated content rather than raw bytes.

### Crucial design choice: information vs cargo

Do **not** mint or transfer goods merely because a packet was heard. In a decentralized system, duplicate/replayed packets otherwise duplicate inventory. A trade needs an authoritative ledger or an explicitly limited trust model.

MVP recommendation: one **session authority** (a designated player-host or optional reachable server) serializes inventory-affecting transactions; peers queue orders while disconnected. A signature authenticates the order author, sequence/nonce prevents replay, and authority-issued settlement confirms it. Relay nodes carry the order without needing to trust or execute it. Design the authority interface so a later replicated/consensus model is possible, but do not promise leaderless anti-double-spend in v1.

Offline solo mode uses a local authority; multiplayer hosted RF mode uses a station host. The host may broadcast compact signed market snapshots and accepted transaction deltas. If the host is unreachable, local UI shows **pending**, never an invented completed trade.

### Example play session

AG7SU discovers a medicine shortage at a remote starport. Its last market bulletin is two hours old, so profit is uncertain. AG7SU reserves 8 medicine units and sends a 200-byte signed offer to Station B. B cannot hear AG7SU today, but Relay C can hear both at different times. C forwards it, B receives it and sends a receipt; later the host validates inventory/payment and sends a settlement. The player gets courier/contract progress from the completed objective, not from retransmitting the offer ten times. Lost packets delay delivery, while expiry and idempotency keep the economy coherent.

### Failure is gameplay, not data corruption

- Unreachable destination: the parcel waits; player can retry or choose relay.
- Stale prices: offer can be rejected or requoted; player sees that risk before sending.
- Expired contract: settlement refuses it, with optional refund/unreserve.
- Duplicate delivery: receiver ACKs again but processes the message only once.
- Host partition: no conflicting completed market actions; queue commands as pending.
- Relay outage: choose another route or wait for band opening.

Do not make bad RF generate random cargo losses. Risk should come from deadlines, price changes, fuel, and chosen game actions, with clear deterministic rules.

## Proposed modules and API seam (future work)

- `transfer_journal`: immutable local event storage, migrations, privacy settings, idempotency.
- `receipt_protocol`: versioned ACKs, capability negotiation, signatures, pending timeouts.
- `achievement_engine`: locally replayable, deterministic rules and progress projections.
- `app_envelopes`: transport-independent game-message encode/decode, TTL, fragmentation integration.
- `spacetraders` (prefer separate package/repository later): game rules, authority/ledger, economy, ship state, contracts, views.

Suggested internal callbacks:
```text
on_object_verified(object, receive_context) -> transfer_event
request_delivery_receipt(message_id, peer) -> pending_receipt
on_receipt_verified(receipt) -> delivery_event
submit_application_envelope(app_id, destination, bytes) -> queued_message_id
on_application_envelope(app_id, source, bytes) -> application_handler
```

Do not expose arbitrary game messages to existing PixelQSO card decoders. Negotiate the envelope type and keep older versions interoperable.

## MVP milestones

1. **PixelQSO collection MVP (no wire change):** define canonical card/edition identity, session/operator/deck grouping, verified-only badges, local journal, export, and tests for deduplication. No network score authority.
2. **Acknowledged transfer MVP:** backwards-compatible receipt capability, timeout/retry handling, matching hash/nonce, and UI status distinctions. Tests cover duplicate, spoofed, stale, lost and reordered receipts.
3. **Space Traders vertical slice, simulated transports:** two starports, four goods, deterministic market ticks, one ship per player, trade contract, single authority, queued order and settlement. Simulate partitions, delays and duplication.
4. **RF integration:** route the small game envelopes through supported Data2G/PixelQSO plumbing; preserve ordinary card traffic and station identification. Add optional store-and-forward relay.
5. **Progression:** missions, ship upgrades, starport building, albums/achievements, interoperable signed event exports; then consider hosted multiplayer and persistent world seasons.

### Acceptance criteria for first playable slice

- Two stations trade while connected, and also succeed after delayed store-and-forward delivery.
- Duplicate/reordered messages never duplicate credits or goods.
- Sender can distinguish queued, delivered, accepted, settled, rejected, and expired.
- Restarts replay durable journals to exactly the same game balances.
- An old PixelQSO peer can continue normal card exchange without interpreting game envelopes.
- No Internet requirement for a two-station hosted session.
- Bandwidth and retry budgets are visible to users and configurable.

## Amateur-radio operation and safety

This is a recreational, noncommercial design. Keep all RF payloads open and inspectable, provide station ID as required by applicable rules, and avoid obscuring message meaning. Digital signatures and integrity checks need careful legal review before operation; do not assume encryption for confidentiality is permitted. Avoid real-money payouts or pecuniary interests, avoid automatic uncontrolled transmissions, and provide operator control of frequency, airtime, duty cycle, and transmit permission. Protocols and jurisdiction-specific regulatory compliance require a separate review before release.

## Open design questions

1. Should achievements be purely local, or voluntarily exchangeable as signed 'passport' artifacts?
2. Should a Space Traders world be one hosted session/season, or many federated worlds with separate economies?
3. Should fictional interstellar distance model ship fuel/time independently of real RF topology? (Recommendation: yes.)
4. How should a temporary authority handoff work when the host leaves? Start with explicit save/export and host migration, not automatic consensus.
5. What maximum game envelope and airtime budget should be targeted for the narrowest Data2G mode?
6. Should collectible cards become tradable game cosmetics? Recommendation: allow copying/display and provenance, but do not claim scarcity for freely reproducible RF artwork.

## Immediate next decision

Build a **two-player, host-authoritative Space Traders prototype with a simulated lossy/delayed transport** before touching the RF wire format. Meanwhile implement PixelQSO collection grouping from existing verified cards. This tests whether communications latency makes interesting decisions without coupling economy correctness to the modem.
