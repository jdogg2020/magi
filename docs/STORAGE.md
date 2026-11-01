# Storage management (0.17.0)

The Storage page measures active camera directories and removed-camera archives
separately, including buffers and event sidecars. It deduplicates hardlinks within
each camera and shows each shared filesystem once. File sizes differ from actual
filesystem allocation; disk free space also reflects OS files, logs and other data.
Unavailable or timed-out directories are explicitly unknown, not zero. A scan has
a five-second budget, runs in an HTTP worker outside capture, and is cached for
30 seconds. It runs only when requested, not during overview polling.

## Cleanup controls

Camera settings retain the existing cap, minimum free disk space and maximum event
age. New optional settings under `storage`:

- `cleanup_margin_bytes` (default 0): when the cap is reached or disk free space
  falls below the floor, attempt to reduce camera use below cap minus margin and
  increase free space to at least floor plus margin. It must be smaller than cap.
- `trash_days` (default 7, greater than 0 and at most 365): expire trashed events
  after this age. Trash can be reclaimed sooner under space pressure.

Defaults preserve existing installations. A camera only prunes its own finished
unprotected events, trash first and then oldest events. Age retention operates
independently of thresholds. Lowering limits or shortening retention can delete
existing unprotected recordings after saving. Cleanup targets are best effort:
protected/in-use recordings remain, even if the target cannot be reached. Capture
pauses if the original cap/free-space floor still cannot be satisfied. Multiple
cameras share physical space, but their caps, floors and cleanup margins remain
per camera; this is not a station-wide reservation guarantee.

Removed-camera archives never run automatic retention, including trash expiry.
They remain accessible in Events after restart. Inventory does not alter them.
Inventory and log maintenance do not change settings or credentials. Explicit permanent deletion controls are described below.

## Logs

Runtime logs remain in the system journal, which uses the Pi's existing journal
rotation policy. No global journal limits are changed.

Install/update logs under `/var/log/magi` have separate management:

- Updater logs rotate at 5 MiB per file with three backups per run.
- A daily `magi-log-maintenance.timer` keeps completed recognized install/update
  logs for at most 30 days, up to 20 files / 50 MiB total, oldest first.
- Active installers/updaters hold an advisory lock; maintenance skips their group.
  Legacy logs without locks are skipped for their first 24 hours. Active and recent
  legacy logs can temporarily exceed these completed-log budgets.
- Unknown files, symlinks and hardlinked logs are not deleted. No recording paths,
  credentials, or system journal files are touched.

The fresh installer and existing dashboard update-helper installation provision
the timer without additional packages. Timer execution is root-only and writable
only within the Magi log directory. Inspect it on the Pi with:

```bash
systemctl status magi-log-maintenance.timer --no-pager
sudo journalctl -u magi-log-maintenance.service -n 20 --no-pager
```

Validation is synthetic: threshold/headroom behavior, protection, trash age,
archive inventory, shared disks, hardlinks, scan failures, active-log exclusions,
log budgets and two-camera dashboard/Chromium behavior. Pi field validation is
separate; no direct connection to the remote Pi is made during development.

## Explicit deletion (0.17.1)

**Edit storage limits** opens the correct camera settings, scrolls directly to the
storage section and focuses its cap field.

Storage offers **Purge all unprotected events** and **Empty all trash**, plus the
same actions per camera. These include removed-camera archives only when explicitly
requested; their automatic-retention exemption remains. Preview shows the camera
scope, count and approximate file size. Confirmation permanently deletes originals,
previews, notes and metadata. It cannot be undone; hardlinks mean the displayed file
size is not a guarantee of reclaimed space. Active capture buffers are untouched.

Preview is bounded to five seconds / 10,000 eligible events and expires after five
minutes. Only previewed event identities can be deleted. The browser confirms in
batches of ten with progress; protection, processing/calibration use, completed
state, volume identity and directory identity are checked again in the camera's
serialized command queue. Newly captured events are excluded. Closing the page or
losing connectivity stops further batches; completed deletions stay deleted. A
lost-response retry for the same step returns the previous result without deleting
another batch. Partial failures are reported; refresh before making a new preview.

**Delete archived camera** is available only after ordinary camera removal. It
previews deletion of that archive's eligible events and, after confirmation,
removes leftover capture buffers and the camera from Storage and Events. Protected,
unfinished or unreadable entries block archive removal. In-use events are rechecked
at execution and prevent removal if retained. Other cameras continue recording.
Additional-camera registry entries and their private configs/backups are removed.
The original camera retains its station configuration and an internal forgotten
marker, because that configuration also holds dashboard login and station defaults;
it no longer appears in the UI, including after restart. Recordings belonging to
other cameras under the same parent directory are never removed.
