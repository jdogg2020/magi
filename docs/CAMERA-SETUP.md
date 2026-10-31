# Camera information and Reolink onboarding

Magi 0.10.0 adds local camera inspection and an **experimental CX820 setup flow**.
The owner has confirmed discovery, factory-new activation, service enabling and
live video on a real CX820. Protocol fixtures, HTTP/TCP simulators and UI behavior
are also tested; overlay removal still needs real-camera validation. Keep the camera password until live video is confirmed.

## Without the Windows Reolink client

1. Power the camera with PoE or its supported supply and connect it to the Pi's
   local network. The network needs DHCP (normally your router). Magi does not
   create a DHCP server, configure Wi-Fi/cellular networking or power the camera.
2. Open Magi Settings → Camera connection → **Set up a Reolink camera with Magi**.
3. The search field defaults to a private IPv4 /24 based on the Pi interface serving
   this dashboard, falling back to the saved camera IP when accessed through localhost.
   Review or edit it (for example `192.168.86.0/24`) and click **Find cameras**.
   Existing typed values are preserved. If no suitable address is available, enter
   the network manually. The suggestion does not claim to detect the interface netmask.
   Choose a result, or enter the private camera IPv4 address manually. Discovery
   checks the Baichuan login-challenge protocol on TCP 9000, not just an open port.
   It does not log in or guess passwords. Only private IPv4 /24 or smaller ranges
   are accepted. Discovery does not cross VLANs or support custom setup ports.
4. For an existing camera, enter its login in Camera connection; a blank password
   reuses the saved password only for the same IP and username. For a factory-new
   or reset camera, select initialization, use `admin`, leave the existing password
   empty, and choose/confirm a new password of 8–31 printable ASCII characters
   without spaces **before inspection**. Inspection only validates these fields; Apply
   creates the password on the camera. Magi never attempts a blank password unless explicitly selected.
5. **Inspect selected camera** reads model, firmware, name and service ports.
   No device settings change. Automatic setup is restricted to identity `CX820`;
   other models and incomplete responses stop here. Review the IP and identity.
6. **Enable services & connect** (or **Create password, enable services & connect**)
   reconnects and checks identity, creates the admin password only when requested,
   verifies the new login, enables only RTSP and local HTTP if disabled, and reads
   the ports back. It then reads channel 0 OSD settings over the local HTTP API,
   disables the watermark, date/time and camera-name display switches with SetOsd,
   and reads them back to verify removal. Camera name text, positions and all other
   OSD fields are preserved. Missing controls, unsupported commands or failed
   readback stop setup with a partial-change message. Other users and camera settings
   are preserved. Existing cameras can repeat Inspect and Apply with initialization
   unchecked and their current password to remove overlays without resetting. It then saves
   the connection through Magi's normal private config/backup mechanism and
   reconnects capture. Camera-reported RTSP/HTTP port numbers are used, with the
   CX820 main/substream path defaults.
7. Wait for **Recording**, a live analysis image, and a recording substream.
   Setup service success alone does not establish working video.

Inspect sessions expire in five minutes, Apply tokens are single-use, and
operations have a 15-second cooldown. Network calls are bounded and run in a
background thread, separate from capture. No mutation retries run automatically.
Settings saves wait while Apply is running; reads do not interrupt recording.

If setup is interrupted, the camera may already have accepted its new password or
service changes. Keep that password, select existing-camera setup and inspect
with it. Magi explicitly reports partial failure; it never resets the camera,
changes a nonblank password, removes other users, opens router ports or enables
cloud/P2P. A firmware may close the connection without acknowledging password
creation; this is reported as uncertain rather than claiming activation succeeded.

HTTP is enabled on the local camera network for the information API. This is
visible in the Apply description. For a trusted-certificate HTTPS deployment,
configure that camera service and select HTTPS/API port in Magi afterward.
Magi verifies HTTPS certificates and does not bypass validation.

If this firmware does not support the implemented setup messages, manufacturer
setup remains a fallback (including the phone app); this release does not claim
universal factory activation or support Reolink account-bound/battery models.

## Camera information

**Camera information & first-time setup → Read camera information** uses the saved
login and chosen HTTP/HTTPS port. It sends Login, a batch of GetDevInfo, GetEnc,
GetRtspUrl, GetTime and GetIsp for channel 0, then best-effort Logout. This action
is read-only. It is on demand with a 30-second cooldown, no cloud connection,
video upload or continuous polling. A missing HTTP service does not stop recording.

The panel shows:

- Model, name, hardware and firmware, when reported.
- Configured main/substream resolution, FPS, codec and bitrate; credential-free
  RTSP paths. It does not automatically replace configured paths.
- Camera wall time and Pi UTC at the sample; estimated camera-minus-Pi offset
  only when timezone/DST information is complete and not near a DST transition.
  Neither clock is independently verified, and the camera clock is not adjusted.
- Available exposure/day-night/white-balance/flip settings, reported as-is.
- For model `CX820`, published nominal FOV 93.2° horizontal / 48.2° vertical,
  explicitly distinguished from API-reported or star-measured geometry. It does
  not mark the station calibrated or overwrite star calibration.
- Main/substream recording status and last confirmed detection dimensions,
  separately from camera-reported configuration. Configured FPS is not measured
  delivered FPS, and API success does not establish video reception.

Unsupported commands appear individually. Timeouts, access refusals, missing
services, invalid responses and certificate failures are readable. Raw API
responses, camera passwords, login tokens, full credential-bearing URLs, UID,
serial numbers and MAC addresses are not exposed. Responses are size limited,
HTTP redirects and environment proxies are disabled, and only allowlisted fields
reach the dashboard. Results remain in memory and clear on settings changes.

## OpenSky credentials.json

Settings → Aircraft & satellite checks has a local file chooser for the OpenSky
credential download. Supported keys: `clientId`/`clientSecret` and
`client_id`/`client_secret`. The browser reads the JSON, validates the fields,
fills the existing credential inputs and selects OpenSky. The original file is
not uploaded; credentials are sent to Magi only on Save Settings and stored with
the existing private configuration. File/credential values are never displayed
in status text. Import does not enable checks or mark pointing calibrated.

## Protocol and manufacturer references

Independent Magi implementation; no external source code is bundled or copied.
The TCP framing, challenge login, service commands (36/37), device information
(80) and user-list messages (58/59) follow the interoperable Reolink Baichuan
wire format. AES-128-CFB128 uses the system OpenSSL EVP implementation via ctypes;
Magi does not implement the cipher or place keys in command-line arguments.

- Reolink Camera HTTP API User Guide v8 (manufacturer-authored guide, mirrored):
  https://hilfe-inkovideo.com/wp-content/uploads/2025/02/Camera-HTTP-API-User-Guide_v8.pdf
- Reolink-authorized reolink-aio protocol implementation/reference:
  https://github.com/starkillerOG/reolink_aio/tree/master/reolink_aio/baichuan
- Baichuan user-list wire semantics reference:
  https://github.com/QuantumEntangledAndy/neolink/blob/master/crates/core/src/bc_protocol/users.rs
- CX820 specifications: https://reolink.com/us/product/cx820/
- Manufacturer initial setup: https://support.reolink.com/sections/206490527/
- Service ports: https://support.reolink.com/articles/900000621783-How-to-Configure-Reolink-Ports-Settings/

## Multiple cameras (0.13.0)

The dashboard header selects the camera whose live view, settings, calibration,
manual event button and archive you are using. **Station cameras** shows all
configured cameras, with recording/detection state, recent analysis images,
event counts and storage usage. All configured cameras run concurrently.

To add the existing second CX820, open **Station cameras**, enter a descriptive
name and click **Add camera**. The new camera's Settings screen opens. Use
Find cameras, select its IP, enter its existing admin password, leave factory-new
initialization unchecked, then Inspect and Apply. Do not initialize a camera
whose password has already been set. New cameras start with empty connection
fields, no ignore zones and reference matching/calibration disabled. Each camera
needs its own pointing calibration. Renaming a camera preserves its stable ID.

Upgrades leave the original `/etc/magi/config.json` and recording directory in
place as Camera 1. Adding/renaming saves the private `/etc/magi/cameras.json`
registry; additional private configurations and backups live under
`/etc/magi/camera-configs/`. Additional footage is isolated under
`<original recording directory>/cameras/<camera ID>/`. Updates preserve all of
these paths. Back up the entire configuration directory, not just config.json.

Each camera has a separate worker, reconnect state, main/substream recorders,
motion tracker, calibration, reference checks and archive. A failed or stalled
camera does not stop the other workers. **Restart selected camera worker** closes
and recovers only that camera's work, preserving its config and recordings.
Settings saves and setup also reconnect only the selected camera. A broken
additional config is reported as a failed worker without taking down Camera 1.
The existing unscoped API/control commands refer to Camera 1; scoped dashboard
requests use `/api/cameras/<id>/...` with the same station authentication and CSRF
requirements. `/api/cameras` lists credential-free camera status. New event
metadata and JSON exports include camera ID and capture-time name. Existing
unattributed events remain in Camera 1's archive.

Storage caps and retention are **per camera**. A camera's storage accounting and
pruning exclude other cameras' directories. All cameras share the physical disk
free-space guard; a quota is not a reservation of disk space. New cameras default
to a cap of at most 10 GB (or the original camera's smaller cap). Configure each
cap and retention in that camera's Settings. CPU, memory, temperature and disk
available are whole-Pi measurements; per-camera tiles report recording state,
detection frame age/format and storage used.

There is a service limit of eight configured cameras. This is not a claim that a
Pi 4 can process eight streams: two independent synthetic sources and both scoped
dashboard flows are tested, but multiple real CX820 streams and Pi performance
still need field validation. Add cameras one at a time and watch native detection
frame counts, dropped frames, temperature and power/throttle flags before
choosing different hardware. Camera removal and a combined cross-camera archive
are not included in this version; each camera's existing event filters and bulk
review operate within its own archive.
