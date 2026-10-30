# Magi development and updates

Magi already runs under systemd. Updating replaces application code and its unit;
it does not reinstall the OS, run pip/apt, modify camera settings, or replace
recordings. A short recording gap occurs while the service restarts. Updates are
manual: development commits do not automatically deploy to the Pi.

## First-time updater setup on the existing Pi

Download the new `magi-prototype.zip` to `~/Downloads`. Extract it into a new
folder, keeping the zip available for the updater:

```bash
mkdir -p ~/Downloads/magi-update-0.4.1
python3 -m zipfile -e ~/Downloads/magi-prototype.zip ~/Downloads/magi-update-0.4.1
cd ~/Downloads/magi-update-0.4.1/magi
sudo bash scripts/install-updater.sh
sudo magi-update ~/Downloads/magi-prototype.zip --check
sudo magi-update ~/Downloads/magi-prototype.zip
```

The bootstrap repairs legacy application-code permissions, verifies an import as
the service account, and installs the update command without restarting recording.
Fresh installations install this command automatically. After the update, reload
the dashboard (Ctrl+Shift+R if necessary) to load the new JavaScript and styles.

## Future updates

Download a selected trusted release, then run:

```bash
sudo magi-update ~/Downloads/magi-prototype.zip --check
sudo magi-update ~/Downloads/magi-prototype.zip
```

With a public repository, the installed command downloads releases directly using
Python's standard library. No GitHub CLI, login, token or account is needed on the
Pi. Run it as your ordinary user; it asks for sudo only for the local update.

```bash
magi-update-github --check
magi-update-github
```

With no version argument it selects the newest published downloadable release,
including preview releases used by this prototype, and prints the selected version.
To select a specific release instead:

```bash
magi-update-github v0.4.2 --check
magi-update-github v0.4.2
```

The command verifies the downloaded checksum before invoking the local updater.
Downloads have size and time limits, and errors leave recording running. A private
repository returns an access error; the public downloader does not accept tokens.
Older prototypes have a GitHub-CLI wrapper; rerun the new `install-updater.sh` once
to replace that wrapper and install the public downloader.

The first command validates the package, repairs legacy installed-source permissions
and verifies the installed version, imports dependencies and validates the
existing config as the actual service account, and checks the systemd unit,
without stopping recording. Missing dependencies stop the update at this stage;
install any documented new OS packages separately. This updater handles schema-1
configuration only and rejects packages that require a newer schema/updater.
Future migrations will need their own explicit, tested backup/restore process.

Permission repair normalizes only known application directories/files to 0755/0644;
it does not change private config files or recordings. Symlinks in application code
are rejected. Fresh installation uses the same repair and verifies an import as
the actual service account before enabling the service.

The actual update obtains an exclusive update lock, stages code, saves the old
code/unit, stops Magi, switches code/unit, and restarts. It requires three
consecutive successful service/dashboard checks and a matching worker version
from the private control socket before marking the update complete. This verifies
startup, not camera reachability, motion detection or a 24-hour hardware run.
Credentials stay on the Pi and are not needed by GitHub.

## Failures and rollback

A startup failure or Ctrl+C during activation restores the previous application
and unit and restarts it. Allow the recovery to finish. If the previous release
also fails its checks, the command reports failure and leaves the recovery journal
for investigation. Update logs are private files in `/var/log/magi/update-*.log`.
They omit config contents and captured subprocess output. Use the normal service
journal for details about a startup failure.

To revert a successful update:

```bash
sudo magi-update --rollback
```

After power loss, SIGKILL or another interruption that prevents recovery, run:

```bash
sudo magi-update --recover
```

Recovery is manual after such an interruption; there is no boot-time rollback
agent. An unfinished transaction blocks subsequent updates until it is recovered.
Previous releases and failed candidates remain under `/opt/magi.releases` for
inspection. They are not pruned automatically in this first updater. Config and
recordings remain where they were; rollback never reverts those files.

The SHA-256 inventory detects altered or incomplete packages, but it is not a
signature or proof of publisher identity. Install only packages from the trusted
private repository or this project's download. The updater executes package code
as part of preflight and installs its service unit; do not use untrusted archives.
The update command does not grant the web service permission to update itself.

## GitHub workflow

Source and the release workflow are maintained at `jdogg2020/magi`. Public
visibility is enabled by the owner in repository settings. License selection
remains pending; changing visibility does not add a license file.

Work on branches and review changes before merging to `main`. The included
workflow runs tests on Python 3.11 and 3.13, checks syntax and builds an archive.
After tests pass on `main`, a new value in `src/magi/__init__.py` creates a
GitHub preview release (for example `v0.4.0`) with the archive and checksum.
Existing releases and tags are left unchanged; bump the version for each new
release. Tag-triggered runs also require a matching source version. Preview
release publication is automatic; installing that release on the Pi is manual.
The workflow does not change repository visibility or deploy to the Pi automatically.

To build locally from the repository root:

```bash
python3 scripts/build_release.py dist/magi-prototype.zip
```

The builder includes an explicit set of source, documentation and test folders;
it excludes local credentials, recordings, caches and git history. The release
contains `magi/release.json` with version, compatibility and file checksums.
After the repository is public, assets can be downloaded anonymously in the
browser or with the public update command.

GitHub CLI command references: https://cli.github.com/manual/gh_release_download
and https://cli.github.com/manual/gh_release_create.
