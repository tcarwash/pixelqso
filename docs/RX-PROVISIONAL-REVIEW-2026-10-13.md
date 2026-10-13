# Receive provisional/copy/pivot review and implementation handoff

Reviewed 2026-10-13 against HEAD `2136f76` plus the current uncommitted changes in `app.py`, `modem_envelope.py`, and `NEXT.md`.

## Scope and evidence boundary

The review below began as a source-only inspection of the recent receive fixes and their interactions with `card_transfer.py`; the implementation checkpoint records the follow-up fixes and regression runs. No transmissions were initiated. The defects describe reachable code paths; they do not establish which path caused the user's latest missing provisional decode. Preserve the existing dirty worktree, including the local TX-button layout fix and the supplied screenshot.

The desired behavior is: show provisional pixels promptly; improve an image from genuine additional evidence; select a newly arriving card even while another is incomplete; retain bounded assembly state for earlier cards; show a verified duplicate with the red DUPLICATE label; add at most one duplicate deck entry per QSO. A change in visible card must not reset the audio receiver or discard other acquired transmissions.

## Implementation checkpoint — 2026-10-13

Source fixes now address all nine findings below. The live receive container was updated in place and its Pixel QSO child restarted; the WebSDR session process and `/recordings` PCM capture remained running. The sibling deployment overlay now contains `app.py`, `card_transfer.py`, `modem_envelope.py`, and the required `ui_theme.py`, so a later image rebuild includes the same code. Local Pixel QSO source is updated, but its running process was deliberately left untouched because a read-only CAT query reported the local radio PTT as keyed. Do not restart that local process until the radio is unkeyed.

Targeted receive regressions pass: header-only worker activity, same-card activity retention/new-card pivot, monotonic placement selection, candidate revisions without repeated-copy votes, absolute acquisition ordering, completion without buffer/cache reset, stale-worker isolation, no-header receive-all fallback, Data2G checked pixels, and version-4 integrity persistence. `tests/test_copy_header.py tests/test_experimental_bursts.py` pass: 26 tests and 47 subtests. The Data2G interleave regression exercises A partial → A refinement → B partial → B complete → duplicate B → C partial, with the same station and numeric card ID reused for different image content. `tests/test_live_preview.py` reports 39 passing tests and two passing subtests, with one unrelated existing failure: `test_starter_card_loads_as_new_personalizable_card` expects an empty library page during startup. Builds, synthetic reports, and a running container do not prove fresh RF reception.

A follow-up audit caught two additional edge cases. Protected card reports and header-only activity reports now use the same `(receive session, mode, content ID)` identity, so another header for the currently previewed card no longer clears its provisional pixels. `MainWindow` now initializes `_active_qso_path`; the running container log had shown a crash when a previously received Data2G card arrived as a duplicate before an active QSO log existed. Regressions cover both cases, including the one-duplicate-per-QSO limit.

Conflicting verified evidence remains an atomic rejection: no pixels, identity selection, or recency state from that report are committed. Both the provisional-preview callback and the final-result path now surface the rejection in receive status and the exchange log. Repeated decoder callbacks for the same identity/error are deduplicated with a 64-entry bound, so a rolling scan explains the rejection without flooding the log. A focused GUI regression verifies the message and deduplication; this identifies the rejection path but does not by itself prove it caused a particular on-air miss.

The running container also exposed a live-worker lifetime failure: Qt logged `Signal source has been deleted` from `LiveDecodeWorker.run`, then its supervisor restarted PixelQSO. The window had not retained the Python `QRunnable` wrapper, which owns the signal object. Live workers are now held by receive token until their completion callback; stale completion releases only its own worker and cannot clear a newer in-flight slot. A regression checks retention and stale cleanup. The container child restarted after this traceback, and the WebSDR bridge and PCM capture continued.

The container UI briefly launched with `ui/advanced_controls=false`; that preference hides the backend selector, receive-all-mode control, and waterfall. The persisted container setting was changed to true and its supervised Pixel QSO child restarted. The user subsequently rebuilt and confirmed the controls were present. This is a UI preference/startup issue, separate from the receive-state fixes.

## 1. Header-only activity becomes an exception in the live worker — high priority, confirmed code defect

**Locations:** `app.py:decode_all_card_backends`, `LiveDecodeWorker.run`, `_live_decode_finished`.

When a protected header exists but payload decoding yields no report, `decode_all_card_backends(..., live_snapshot=True)` returns a report containing `receive_activity`, `receive_profile`, and `pivot_receive`, with no `card`. The worker unconditionally evaluates `result["card"].get("exact")`. This raises `KeyError('card')`, which the outer handler converts into an error with no report. The GUI's explicit activity/pivot handling therefore never sees this fallback event. This directly breaks the transition intended to announce a new card before its pixels exist.

**Fix steps:**

1. Handle activity reports explicitly before reading card fields; emit them as incomplete successful outcomes.
2. Keep malformed reports distinct from valid activity events. Do not turn arbitrary missing-card reports into silent success.
3. Preserve the protected identity and acquisition timestamp on the activity event; they are needed for issue 2.
4. Keep the progress, preview, and finished signal contracts consistent.

**Regression:** route an acquired header with an incomplete payload through the actual `LiveDecodeWorker.run` signal path. Assert one incomplete activity result, no error, correct mode/identity, and a visible waiting state. A later partial result must show pixels.

## 2. Unconditional activity pivot can erase the same card's provisional preview — high priority, latent behind issue 1

**Locations:** `decode_all_card_backends` fallback, `_show_receive_activity`, `_receive_activity_expired`.

Every payload-incomplete fallback sets `pivot_receive=True`, even when the newest protected identity is the same as the currently displayed image. `_show_receive_activity(pivot=True)` clears the current assembly selection and hides its preview. The fallback contains no identity, so the UI cannot distinguish a new card from another incomplete attempt at the same card. Once issue 1 is fixed, this can make provisional images disappear on a non-improving snapshot. Repeated cached-header activity also restarts the 12-second timer despite no fresh evidence.

**Fix steps:** track the active protected identity and the acquisition/event revision. Pivot only on an actual identity transition or an explicit new receive encounter. Preserve the same image's provisional pixels during unsuccessful decode attempts. Start/extend activity timing on new evidence, not on every reread of a cached header. Expiration may stop the activity indication but must not discard useful provisional pixels.

**Regression:** partial A → incomplete attempt at A → partial A retains its preview and coverage. Partial A → header B shows B's waiting state; B pixels replace it. A cached header with no new samples does not keep activity alive forever.

## 3. Alternative decoding placements can downgrade a verified result — high priority, confirmed code defect

**Locations:** `decode_all_card_backends.consider_preview` and its placement loop.

For one protected identity, the code tries multiple audio placements. `consider_preview` accepts any non-activity report whose acquisition timestamp is greater than or equal to the current one. These placement attempts share the same timestamp. A verified result from an earlier placement can therefore be replaced by a later provisional result; even a stronger provisional report can be overwritten by a weaker one. The former early exact-result return is absent. The final outcome then reports the last accepted result rather than the strongest result for the selected identity.

**Fix steps:** separate identity selection from evidence quality. First choose the active identity by acquisition chronology, then retain its strongest result: verified whole-card result first, compatible checked regions next, candidate coverage last. Never use placement iteration order as evidence quality. Once the selected identity is verified, return that result or retain it while other work runs. Avoid emitting regressive preview updates.

**Regression:** same identity and acquisition, placements produce exact → partial, partial → exact, and high-coverage → low-coverage. All orders must retain the strongest evidence. A newer different identity must still become active even if an older identity is exact.

## 4. Completion/duplicate handling discards unrelated buffered transmissions — high priority, confirmed destructive state transition

**Locations:** `_live_decode_finished`, `_reset_receive_window`, `_clear_rx_bytes`.

On every manual-mode complete result, including a duplicate, `_reset_receive_window` clears the entire rolling audio buffer and replaces `CopyCaptureCache`. A decode worker operates on a snapshot while capture continues. The buffer can already contain the next card's header or partial payload by the time the worker completes. Clearing it can lose that next acquisition. Preserving the preview only changes presentation; it does not preserve audio or copy state.

**Fix steps:**

1. Separate completing/publishing a card from restarting capture.
2. Keep an absolute sample cursor and retain unconsumed audio, protected headers, and incomplete payloads.
3. Mark a completed encounter as handled so cached exact results do not repeatedly trigger finalization. Keep duplicate display/deck policy separate from decoder scheduling.
4. Retire evidence by bounded age/consumption rules rather than deleting all identities on one completion.
5. Reserve full reset for an actual input/session/backend change; invalidate stale worker results with a receive-session token.

**Regression:** delay completion of worker A while B's header and payload arrive. Publishing A or duplicate A must leave B decodable. Repeat with A's trailing repeat followed immediately by B.

## 5. Acquisition time has inconsistent units/meaning across layers — high priority, confirmed metadata mismatch

**Locations:** `newest_protected_acquisition`, `CopyCaptureCache.materialize`, `modem_envelope.decode_capture`, `CardPreviewAssembler.merge`.

The cache correctly supplies `diagnostics.acquisition_start_sample` as an absolute 8 kHz acquisition coordinate. `newest_protected_acquisition` uses it. But `decode_capture` overwrites report acquisition time with `max(copy.start)`, which is an offset in the concatenated cache materialization, not necessarily chronological RF time. The legacy branch likewise uses `copy.start`. The preview assembler then looks in a different place entirely: `card.acquisition.start_sample`, ignoring the common-header acquisition field. These coordinates cannot safely identify new physical copies or order stale results.

**Fix steps:** introduce a documented receive-session identifier plus absolute acquisition time/sample coordinate with its rate. Keep payload-array offsets separate. Preserve acquisition metadata through envelope decoding, worker events, assembly, and presentation; use the same physical-copy identifier throughout. Do not compare sample coordinates from different sessions without a session key. For legacy reports without this metadata, use an explicit fallback with limited guarantees.

**Regression:** interleave cache insertion A, B, then a later A copy. Rearranging concatenated payload storage must not change chronological selection. Resetting a session must not make a new acquisition look older than a previous session's coordinate.

## 6. Provisional pixel voting counts snapshots inconsistently — high priority, confirmed algorithm limitation

**Location:** `CardPreviewAssembler.merge` (`more_evidence`, `pixel_votes`, `copies`, `window_id`).

Candidate votes are admitted when copy count increases, a receive window changes, or `card.acquisition.start_sample` changes. This creates two problems:

- A growing snapshot of the same physical copy can improve an already covered but unverified pixel without increasing `received_copies`; that correction is ignored.
- A report combining N copies is treated like an independent pixel observation weighted by the increase in copy count. It may include old evidence already counted. A new receive window also authorizes another vote even if it replays the same evidence.

The count is a maximum, not an identity set. Equal counts across separate acquisitions/modes do not establish whether evidence is new. Repeated callbacks should not strengthen confidence.

**Fix steps:** represent physical copies and their revisions explicitly. Replace the contribution of a revised copy rather than append another vote. If the decoder only supplies an aggregate estimate, replace that identity's aggregate candidate layer rather than invent independent votes. Preserve verified pixels and reject conflicting verified data as today. Keep soft combining inside the modem where likelihoods and copy provenance are known.

**Regression:** repeat an identical report 20 times (no stronger confidence); improve one copy's candidate estimate (visible correction); add a genuinely independent second copy (new evidence); replay overlapping aggregates (no double counting); revise provisional pixels beneath checked regions (checked pixels unchanged).

## 7. Cached replay and fresh arrivals are indistinguishable to visible-card selection — design gap with concrete vulnerable paths

**Locations:** `CardPreviewAssembler.merge`, `_data2g_rx_frame`, `_live_decode_preview`, `_live_decode_finished`.

Every accepted assembly merge sets `current_key` to the incoming identity. That is appropriate for a genuinely new arrival, but there is no acquisition/session watermark distinguishing a delayed result or cached retransmission from the current arrival. Data2G can return an already complete cached assembly as soon as a repeated fragment arrives, while another image is partial. Local workers use `auto_generation`, which is not a dedicated capture-session identifier, and clear `live_decode_inflight` before checking generation. Review restarts/reconnects for stale completion races.

**Fix steps:** separate bounded per-image assembly storage from active receive encounter and displayed preview. Attach session/encounter provenance to results; stale results may improve stored evidence but must not seize the current preview. For Data2G, use valid frame arrival order and available transport identity rather than inventing RF timestamps. Explicitly define when a fresh repeat of A after B counts as a new active encounter. A genuinely new transmission of A should still be able to become active; do not permanently blacklist old identities. Use a per-worker token when releasing the in-flight slot.

**Regression:** partial A → partial B → delayed worker A does not replace B; partial A → partial B → genuinely fresh A can select A; an old worker completing after reconnect neither changes the display nor releases the new worker's busy state.

## 8. Whole-card verification can regress after a weaker report — confirmed state-merging risk

**Locations:** `CardPreviewAssembler.merge`, `CardAssemblyService.assess`, `preview_snapshot`.

Verified pixels are retained, but `state['card'].update(incoming_card)` overwrites integrity metadata with the latest report. For version-4 avatar reports, the service requires both accumulated checked blocks and `whole_raster_valid`; a later partial report can replace a prior true whole-raster flag with false. Other whole-card flags/exact values can similarly be replaced. Stored pixel coverage and metadata can consequently disagree. Conversely, all checked blocks must not by themselves promote a protocol that requires whole-raster verification.

**Fix steps:** retain validated completion evidence bound to immutable content identity/raster. Preserve it across weaker reports of the same content; reject conflicting verified evidence. Never blindly OR integrity flags across differing content or infer whole-card verification from candidate coverage. Ensure stored snapshots and returned reports share the same authoritative verification result.

**Regression:** whole-card-verified A → partial A stays verified; accumulated checked blocks without required whole-raster proof stay provisional; conflicting checked pixels fail without changing current selection or stored evidence.

## 9. Live receive-all behavior depends on the experimental-visibility flag — configuration-dependent reachability risk

**Locations:** `LiveDecodeWorker.run`, `decode_all_card_backends`.

In auto mode the worker passes `include_experimental or mode != 'auto'`. When auto mode is active and the visibility checkbox is false, protected-header decoding is skipped inside `decode_all_card_backends`, and `live_snapshot=True` returns before the blind decoder loop. This can yield no decoding even though the worker already found an announcement. Determine whether UI/backend policy permits that combination before calling it the cause of an observed miss.

**Fix steps:** derive the allowed RX mode set from backend capability and receive configuration. Keep menu visibility separate from permission to decode enabled formats. Route recognized allowed headers regardless of whether experimental choices are shown in the menu; intentionally disallowed formats should have an explicit status.

**Regression:** exercise receive-all with the checkbox both ways, and selected-mode receive with enabled/disabled formats. No allowed modem should silently lose its live decoding path.

## Suggested implementation order

1. Add focused regressions for issues 1–4 before changing their contracts. Reuse `tests/test_live_preview.py`, `tests/test_copy_header.py`, and `tests/test_experimental_bursts.py`; inspect existing assertions because some encode older UI behavior.
2. Define session, physical-copy, content, and encounter identities; normalize acquisition metadata (issue 5).
3. Fix activity routing and conditional pivoting together (issues 1–2), then make result selection monotonic within one identity (issue 3).
4. Decouple completion from receive-buffer/cache reset (issue 4), with bounded retention and handled-encounter tracking.
5. Repair revision accounting and immutable verification state (issues 6 and 8).
6. Implement explicit active-encounter selection and stale worker handling across local and Data2G paths (issue 7), then resolve configuration gating (issue 9).
7. Add structured diagnostic events for acquired identity/copy, absolute acquisition time, payload growth, decoder attempt, candidate/verified coverage, selected identity, pivot reason, suppressed stale result, and duplicate deck admission. Log counts/identifiers rather than entire rasters. These events should explain whether a missing provisional is acquisition failure, decoder failure, rejected evidence, or hidden UI state.

## Acceptance sequence and deployment

Use deterministic reports/frames first, then saved real audio, then operator-controlled on-air validation. Cover A partial → more A → B partial → B complete → duplicate B → C partial, and switch modem for a retransmission of the same content. Include two different images with the same station and reused card ID; protected content identity must keep them separate. Include a missing/damaged opening header and a recoverable closing marker.

The sequence must prove: prompt provisional rendering, no downgrade of checked pixels, no snapshot double counting, genuine pivot to another card, no completion-induced loss of the next header, continued decoding after a duplicate, red DUPLICATE preview, and at most one duplicate deck entry per QSO. Verify the QSO boundary resets the quota. Keep decoder correctness, rendered GUI behavior, and fresh RF success as separate evidence.

After source fixes, update `NEXT.md` and this handoff with actual results. The user explicitly requires every RX-side update to be installed in the running receive container and restarted as needed. Inspect the sibling `/home/tyler/dev/pixelqso_stream` deployment, effective image/overlay and persistent mounts, then deploy the reviewed source and confirm the running files match. Update/restart the local application when needed for its UI/decoder changes. Keep recordings and received images intact. Testing uses the Utah WebSDR audio stream only; do not start a YouTube stream. The user presses transmit. Report remaining on-air validation honestly rather than treating a build or synthetic replay as RF proof.
