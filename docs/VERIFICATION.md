# Magi 0.11.0 event management and candidate profiles

Synthetic tests cover brief broad flashes with a stable baseline, rejected slow/
alternating illumination, bright trails, straight point tracks, curved-track
rejection and ignore zones. Storage tests exercise private sidecar annotations,
full-archive filtering/paging, per-event bulk failures, persisted protection,
trash/restore/expiry, invalid paths, calibration/worker guards, metadata-only
exclusions, reference policy snapshots, cleanup recovery and preserved legacy,
manual, mixed and annotated footage. HTTP tests cover CSRF and label queries;
the dashboard harness round-trips rows, notes, reviewed status, bulk protection
and trash/restore through a running synthetic service. Real-world accuracy and
Pi CPU/storage measurements are not established. See EVENTS.md.

# Magi 0.10.0 camera setup and information

HTTP simulation covers token login, the read-only command allowlist, logout,
credential-free responses, optional fields and conservative clock conversion.
TCP simulation covers challenge/login framing and fragmented responses; AES
is checked against an independent OpenSSL ciphertext vector. Setup fixtures
exercise read-only inspection, model restrictions, single-use/expired tokens,
identity changes, password verification, preserving other users, XML escaping,
partial failure without retries and private completion handoff. UI checks cover
information display, setup edit invalidation and local OpenSky JSON import.
Actual CX820 firmware activation and Pi/network behavior remain untested.

# Magi 0.9.0 assisted star calibration

Synthetic regression tests recover pointing, roll and both fields of view,
including zenith/wraparound geometry. Wrong identities, clusters/collinear
layouts, insufficient points, duplicate stars, malformed tokens, changed images,
expired sessions and failed re-fit reuse are rejected. Tests cover no-change
preview, atomic application, reload validity, capture provenance, different
historical calibration gating, native horizon-axis conversion through Skyfield
and bundled catalogue values. The authenticated HTTP integration exercises
frame loading and invalid-fit rejection.

The dashboard DOM harness exercises saved-frame selection, local star catalogue
loading, point entry, zoom, clearing and rejection of a collinear fit, alongside
the existing login/review/settings tests. It does not validate browser layout or
actual night-sky image accuracy. No physical camera, distortion, atmospheric,
proper-motion or Pi performance validation is claimed. See STAR-CALIBRATION.md.

# Magi 0.8.0 recorded-track visibility

The dashboard DOM harness passes image-first viewing, selected-image markers,
color-video track availability, exact-mask suppression on color video, tracker
toggling, timeline seeking and the existing replay pixel controls. A separate
Node regression test covers coordinate normalization, segment gaps, malformed
or missing timing, suppression, path splitting and reticle interpolation/expiry.
GitHub runs this geometry test alongside the Python suite on 3.11/3.13.
The authenticated HTTP integration now verifies the substream timeline route.

This validates synthetic data and control behavior, not visual browser layout or
real camera overlay alignment. Color video and selected-image positions remain
estimated. The unpublished stream-probe draft is not included in this release.

# Magi 0.7.1 image-first event review

Event thumbnails open an inline native-resolution still without autoplay. Play
starts the selected substream/detection video at zero. The dashboard harness
checks still-first opening, repeated play/reopen, returning to the image,
changing quality without autoplay, full-frame viewing and detection overlays.
The authenticated HTTP test checks that full-resolution JPEGs no longer carry
an attachment header. Recording/detection behavior is unchanged from 0.7.0;
the tentative decoder/probe rewrite remains an unpublished draft.

# Magi 0.7.0 native substream preview

The full local suite passed 80 tests (one Unix-socket restriction skip).
Focused tests confirm a copied substream MP4 retains native dimensions/frame rate
and encoded-packet SHA-256 payloads from its H.264 input. Main capture remains
alive/buffering when the substream fails and reconnects. A real decode test confirms
analysis uses native geometry/rate despite conflicting legacy dimensions/FPS.
The motion-to-browser integration covers original, native substream preview,
selected thumbnail, analysis replay/overlays and authenticated routes. A regression
check leaves a pre-start archived event idle across automatic status snapshots.

A real synthetic 4K HEVC original still produces a native full frame and color
object crop, unchanged original hash, and no generated video. The dashboard's DOM
harness passed native-substream default selection and separate pixel overlays,
plus the existing sign-in/settings/save/ignore-zone behaviors. This does not
validate browser visual layout, real camera RTSP behavior or actual Pi CPU load.
The original 0.6 full-video transcode has been removed. Native-resolution detection
can cost more than the previous reduced-resolution detector; field measurement
remains necessary. GitHub runs the complete suite on Python 3.11 and 3.13 before
publishing a release.

# Magi 0.6.0 original-derived review media

A real synthetic 3840×2160 HEVC clip produced a native-size selected JPEG, a
smaller native-pixel color crop and a decoded/playable 1280×720 H.264/15 FPS review.
The original's SHA-256 stayed unchanged. Focused tests also cover crop bounds,
segment timing with gaps, using analysis sample time, changed-volume rejection,
and conversion timeout cleanup. The authenticated motion-to-event integration
checks the new HD byte-range route, full-frame JPEG, media metadata and render
queue endpoint, while preserving the existing overlay replay and original routes.

The actual dashboard JavaScript passed its DOM harness against a running synthetic
service, waiting for HD generation and checking HD default selection, suppression
of mismatched overlays, and detection replay toggles. Existing setup/save/mask
checks also passed. This validates behavior, not browser visual layout.

Full GitHub verification runs on Python 3.11/3.13 before release. No actual Pi
render throughput, sustained capture headroom or real-object crop placement has
been measured here. Frame selection is a bounded contrast/sharpness heuristic;
matching main/substream field of view and approximate timing are recorded as
assumptions. The original and detection replay remain fallback evidence.

# Magi 0.5.0 ignore-zone parity milestone

Six detector/config regression checks pass: old configs default to no zones,
invalid/bounded zones are rejected, masked motion cannot trigger while outside
motion still can, zones scale with resolution, exposure rejection uses the
unmasked area, and fully masked images are safe. Raw changed pixels and input
frames remain intact. An authenticated API test covers save/reload and atomic
rejection of invalid geometry.

The actual dashboard JavaScript passed its DOM harness against a synthetic HTTP
service, including pointer drawing, removal, coordinate entry, clearing and save
payloads, alongside existing login, live image and event review checks. JavaScript
syntax and HTML ID checks pass. Browser rendering, touch hardware and real-sky
mask placement still need validation on the Pi.

The full suite runs in GitHub on Python 3.11 and 3.13 before release. Local suite
runs exclude the installer signal tests when the execution host intercepts their
Ctrl+C simulation; those tests remain enabled in GitHub. See the parity roadmap
for documented upstream features and remaining gaps; 0.5.0 is a first delivery,
not a claim of overall detection or platform parity.

# Magi 0.4.2 public release downloader

The GitHub update command now uses unauthenticated Python HTTPS requests, without
GitHub CLI, credentials or tokens. It selects a requested tag or the newest
published downloadable release (including previews), validates the SHA-256 file,
and invokes the local updater only after verification. Downloads are bounded.
Installer/bootstrap install the new Python helper and replace the old CLI wrapper.
The release workflow no longer requires private repository visibility; it still
requires verification before publishing, and installation on the Pi stays manual.

Five new fixture tests pass: no Authorization header or gh dependency; explicit
tag selection; checksum rejection before writing a package; size limits; and
invalid-version rejection before making a network request. Actual anonymous
GitHub downloading remains pending the owner changing repository visibility.

# Magi 0.4.1 installation permission regression

The Pi's original source was mode 0700/0600 and unreadable by the dedicated
service user. The installer, updater bootstrap and preflight now share explicit
application-only permission repair. Installer/bootstrap also verify an import as
the service account. Both --check and update verify the installed version before
stopping recording; bootstrap no longer prints a copyable fictitious ZIP path.

Thirteen updater tests pass, including private-source repair, idempotence,
preservation of private config permissions, symlink rejection, and checking the
installed import before candidate dependencies. Existing rollback/recovery checks
also pass. Full GitHub verification runs on Python 3.11 and 3.13 before release.
GitHub also runs a disposable root-owned install probe: importing as an
unprivileged account must fail before repair and succeed afterward, while its
private config stays unreadable. This privilege switch is blocked in the local
execution environment, so it is verified on the GitHub runner. The new bootstrap
still needs to be exercised on the user's Pi.

# Magi 0.4.0 update workflow

Full suite: **55 passed, one skipped** (56 tests in 56.552 seconds). The skipped
Unix-control-socket integration test remains blocked by this environment. After
adding durable directory synchronization, the ten focused updater tests passed
again. Installer/update/fetch shell syntax, Python compilation, release building,
inventory checks and safe extraction also passed.

Updater tests exercise release checksums and traversal rejection; readable code
permissions; preflight failure without stopping recording; successful activation;
startup failure and Ctrl+C rollback; successful manual rollback and roll-forward;
interruption before/after the old-code move; and blocking unfinished transactions.
These tests use actual temporary filesystem operations with simulated service
controls and health checks. They do not represent an actual root/systemd upgrade
on a Pi. The GitHub workflow is prepared but has not run in a remote repository;
private release fetching is syntax-checked and awaits a published private release.

The user confirmed the earlier prototype works on the Pi after repairing source
permissions. That is separate from updater validation: actual Pi update, rollback,
power-loss recovery and continued camera recording still require hardware checks.
Updates preserve external config/recordings and reject config-schema migrations.

# Magi 0.3.4 settings feedback

Successful settings saves now open the camera overview, scroll to the top, fetch
the live frame and show connection/recording feedback. Errors keep the Settings
view open. A sticky save toolbar precedes the form; optional aircraft/satellite
fields use a collapsed details section. Setup-message Settings links open the
existing settings view.

The actual dashboard JavaScript passed its DOM harness against a synthetic HTTP
service, including setup navigation, successful save navigation/status and failed
save retention. Save responses were mocked for these UI checks; the existing
service supplied status, settings, video and event data. JavaScript syntax and
HTML ID/container checks passed. Browser layout and the user's live camera
connection remain to be verified on the Pi.

# Magi 0.3.3 source permission repair

The Pi reported `No module named magi` even though the unit sets PYTHONPATH.
Installer copies previously retained private source permissions and umask 077.
Application directories now receive mode 0755 and files mode 0644; credentials
and recordings retain their private permissions in separate locations.
Shell syntax and a private-source copy/permission smoke check passed. The user
must restart the Pi service to verify the startup failure is resolved there.

# Magi 0.3.2 installer diagnostics

Installer output is saved in mode-0600 per-run logs with timestamped named steps,
15-second heartbeats, bounded package/service operations, and explicit failure or
cancellation exit codes. Ctrl+C stops the active step's process group, escalating
from interrupt to termination and kill if needed. Interactive credential output
bypasses the log. Package steps preserve existing package configuration and avoid
unattended prompt hangs. Rerun/repair instructions are documented in START-HERE.

Five focused tests passed: streamed output/heartbeat and failure code; timeout
cleanup of interrupt/termination-resistant descendants; direct runner cancellation;
shell Ctrl+C propagation; log privacy, permissions and failure context. Installer
shell syntax and dry-run checks passed. These tests use harmless subprocesses;
actual apt/dpkg cancellation and installation on Pi hardware still need field
verification. Completed installation steps are not automatically rolled back.

# Magi 0.3.1 installation update

The installer now creates `/mnt/magi-ssd` and supports ordinary-directory
recording on the Pi SD card. Fresh directory installs reserve 2 GB free and
choose a cap up to 10 GB, limited to half the remaining space. Fresh mounted
drives get strict mount protection; upgrades preserve existing policy.
`--require-mounted-ssd` explicitly requires an actual mount.

Five additional storage tests passed: usable directory storage with identity
protection, small-card budgeting/low-space rejection, strict unmounted refusal,
automatic mounted-drive protection, and upgrade policy preservation. Installer
shell syntax, both dry-run modes, Python compilation and systemd verification
also passed. The earlier full 0.3 verification below remains the baseline; this
installation update was checked with these targeted tests, not another complete
hardware or live-camera run.

# Magi 0.3 verification

Environment: x86_64 Linux, Python 3.12.14, FFmpeg 6.1.1, NumPy 2.3.5,
Pillow 12.3.0. Optional astronomy: Skyfield 1.54, SGP4 2.25, jplephem 2.23.

**35 tests passed; one Unix-control-socket test skipped** because this execution
environment prohibits Unix sockets. HTTP/TCP tests ran normally. Browser logic
was exercised with the actual JavaScript and a lightweight DOM harness against
a running synthetic Magi service. This verifies behavior, not visual rendering.

New checks verify:

- Exact difference masks, broad-change flags, bit-packing round trips and saved
  review frame indices, without changing detector input pixels.
- Automatic motion -> original clip + clean review video + compressed pixel-mask
  sidecar -> authenticated HTTP download and successful decoding.
- Replay-side overlay loading, drawing and highlight toggles in the DOM harness.
- WGS84 observer/aircraft directions and camera projection; matching multiple
  points by position and time; rejection of reversed motion and stale positions.
- Separate disabled, calibration-required, unavailable/partial coverage and
  possible-match outcomes; unavailable data never becomes a no-match claim.
- Local ADS-B geometric-altitude units and rejection of barometric-only records.
- OpenSky OAuth exchange, token/snapshot caching and rate-limit backoff using
  mocked official-format responses, without exposing credentials or tokens.
- Real offline Skyfield/SGP4 propagation using the published historical ISS OMM
  example at its own epoch; stale epochs yield partial coverage, not a no-match.
- Authenticated check-again API and report retrieval; API credentials are redacted
  from settings responses. Previous recorder/auth/config/API tests also pass.

Shell syntax/dry runs, systemd unit verification, Python compilation, JavaScript
syntax and HTML/JavaScript ID consistency passed. The exact optional astronomy
versions are recorded in requirements-identification.lock. Missing optional
Skyfield causes its offline test to skip and runtime satellite checks to report
an unavailable dependency; it does not prevent recording.

## Limits of this verification

No live OpenSky account, live receiver feed, current CelesTrak download or actual
observed sky pass was validated. Provider behavior was exercised with fixtures;
orbital propagation was exercised with real library calculations. Camera pointing,
lens distortion, clock offset and matching tolerance still need field validation.
No actual Pi installation, ARM64 run, reboot, SSD mount-loss test under systemd,
thermal benchmark or 24-hour hardware soak has been performed from this environment.
No software or camera settings were changed on the user's Pi.

## Test output

```text
test_adsb_units_and_geometric_altitude_requirement (test_motion_identification.MatchingTests.test_adsb_units_and_geometric_altitude_requirement) ... ok
test_center_pixel_and_wgs84_zenith (test_motion_identification.MatchingTests.test_center_pixel_and_wgs84_zenith) ... ok
test_fresh_adsb_direction_and_time_match (test_motion_identification.MatchingTests.test_fresh_adsb_direction_and_time_match) ... ok
test_invalid_geometry_is_rejected (test_motion_identification.MatchingTests.test_invalid_geometry_is_rejected) ... ok
test_missing_calibration_and_disabled_are_explicit (test_motion_identification.MatchingTests.test_missing_calibration_and_disabled_are_explicit) ... ok
test_network_failure_never_becomes_no_match (test_motion_identification.MatchingTests.test_network_failure_never_becomes_no_match) ... ok
test_opensky_does_not_substitute_barometric_altitude (test_motion_identification.MatchingTests.test_opensky_does_not_substitute_barometric_altitude) ... ok
test_opensky_oauth_recent_history_and_token_cache (test_motion_identification.MatchingTests.test_opensky_oauth_recent_history_and_token_cache) ... ok
test_opensky_rate_limit_is_unavailable_not_unidentified (test_motion_identification.MatchingTests.test_opensky_rate_limit_is_unavailable_not_unidentified) ... ok
test_reversed_motion_does_not_match (test_motion_identification.MatchingTests.test_reversed_motion_does_not_match) ... ok
test_satellite_offline_omm_propagation_and_epoch_age (test_motion_identification.MatchingTests.test_satellite_offline_omm_propagation_and_epoch_age) ... ok
test_exact_difference_mask_and_large_change_flag (test_motion_identification.MotionTests.test_exact_difference_mask_and_large_change_flag) ... ok
test_saved_pixel_masks_round_trip_and_review_indices (test_motion_identification.MotionTests.test_saved_pixel_masks_round_trip_and_review_indices) ... ok
test_failed_login_is_rate_limited (test_prototype.AuthenticationTests.test_failed_login_is_rate_limited) ... ok
test_password_and_session_expiration (test_prototype.AuthenticationTests.test_password_and_session_expiration) ... ok
test_exposure_flash_and_broad_changes_are_suppressed (test_prototype.DetectionTests.test_exposure_flash_and_broad_changes_are_suppressed) ... ok
test_long_gap_cannot_join_unrelated_tracks (test_prototype.DetectionTests.test_long_gap_cannot_join_unrelated_tracks) ... ok
test_moving_small_object_qualifies (test_prototype.DetectionTests.test_moving_small_object_qualifies) ... ok
test_static_frame_and_sensor_blips_do_not_qualify (test_prototype.DetectionTests.test_static_frame_and_sensor_blips_do_not_qualify) ... ok
test_complete_motion_to_browser_and_original_download (test_prototype.PrototypeTests.test_complete_motion_to_browser_and_original_download) ... ok
test_invalid_settings_do_not_replace_working_config (test_prototype.PrototypeTests.test_invalid_settings_do_not_replace_working_config) ... ok
test_web_blocks_bad_host_cross_origin_and_filesystem_escape (test_prototype.PrototypeTests.test_web_blocks_bad_host_cross_origin_and_filesystem_escape) ... ok
test_unconfigured_camera_keeps_setup_available_without_disk_writes (test_prototype.SetupTests.test_unconfigured_camera_keeps_setup_available_without_disk_writes) ... ok
test_mount_guard_detects_missing_marker (test_recorder.ConfigTests.test_mount_guard_detects_missing_marker) ... ok
test_secret_permissions_and_escaped_credentials (test_recorder.ConfigTests.test_secret_permissions_and_escaped_credentials) ... ok
test_cli_runs_status_trigger_and_shutdown_without_secrets (test_recorder.RecorderTests.test_cli_runs_status_trigger_and_shutdown_without_secrets) ... skipped 'execution environment prohibits Unix sockets; run this test on the Pi'
test_disconnect_marks_partial_clip_and_reconnects (test_recorder.RecorderTests.test_disconnect_marks_partial_clip_and_reconnects) ... capture interrupted (camera_disconnected); retry scheduled
ok
test_hevc_main_stream_is_preserved_and_playable (test_recorder.RecorderTests.test_hevc_main_stream_is_preserved_and_playable) ... ok
test_overlapping_triggers_merge (test_recorder.RecorderTests.test_overlapping_triggers_merge) ... ok
test_restart_recovers_pinned_footage (test_recorder.RecorderTests.test_restart_recovers_pinned_footage) ... ok
test_retention_prunes_oldest_finished_event_but_protects_active (test_recorder.RecorderTests.test_retention_prunes_oldest_finished_event_but_protects_active) ... ok
test_ring_prunes_old_segments_during_continuous_capture (test_recorder.RecorderTests.test_ring_prunes_old_segments_during_continuous_capture) ... ok
test_second_worker_cannot_delete_first_workers_buffer (test_recorder.RecorderTests.test_second_worker_cannot_delete_first_workers_buffer) ... ok
test_stale_buffer_not_available_during_reconnect (test_recorder.RecorderTests.test_stale_buffer_not_available_during_reconnect) ... capture interrupted (test_outage); retry scheduled
ok
test_storage_limit_stops_capture_and_retains_pinned_sources (test_recorder.RecorderTests.test_storage_limit_stops_capture_and_retains_pinned_sources) ... capture interrupted (storage_limit); retry scheduled
ok
test_trigger_preroll_postroll_and_encoded_packet_identity (test_recorder.RecorderTests.test_trigger_preroll_postroll_and_encoded_packet_identity) ... ok

----------------------------------------------------------------------
Ran 36 tests in 39.374s

OK (skipped=1)
```

## Dashboard logic output

```text
Dashboard behavior passed: sign-in, status, settings load, live frame, event cards, preview dialog, sign-out.
```


## Faster GitHub verification and releases

Run the checks relevant to a local change, then let the PR workflow run the full
Python 3.11/3.13 suite, root/systemd probes and rendered synthetic dashboard.
Both matrix jobs must pass before a separate job records the exact tested Git
source tree as a 30-day artifact. A push to main or a release tag looks for that
proof from a successful run of the same workflow. It reuses verification only
when the full source tree matches, including workflow files and dependencies.
Artifacts are inspected as metadata; their contents are not executed.

Missing, expired, failed or mismatched evidence, direct/squash/rebased pushes
without matching evidence, and GitHub API errors run the full suite again.
Releases still require successful verification or matching successful proof,
and keep the existing version, inventory, checksum and no-overwrite checks.

Python downloads are cached by both lockfiles and interpreter. Pinned Playwright
and Chromium downloads are cached by runner platform and browser version. The
actual browser is launched to check system libraries; missing libraries are
installed before tests. FFmpeg installation skips apt when both tools are
already available. Main warms download caches in parallel with publication,
since PR-scoped caches cannot seed future PRs. Warm jobs do not run tests.
Superseded runs of the same PR are cancelled; main/tag release runs are retained.
No Pi installer timeouts, settings, credentials or recordings change.

Group related small changes in a single PR/version where practical. A workflow
change alone does not need an application version bump or a separate Pi update.
