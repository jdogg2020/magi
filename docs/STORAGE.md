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
There is no new permanent-delete action. Settings, credentials and camera identity
are unchanged by inventory and log maintenance.

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
