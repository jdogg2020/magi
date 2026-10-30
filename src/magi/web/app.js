'use strict';
const $ = id => document.getElementById(id);
let connectionStartedAt = 0;
let ignoreZones = [], zoneStart = null, zoneDraft = null, zoneFrameURL = null, zoneFrameReady = false;
let csrf = '', settingsLoaded = false, busy = false, frameURL = null, clipSignature = '';
const bytes = n => n == null ? '—' : n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : `${(n / 1e6).toFixed(0)} MB`;
const number = (n, suffix = '') => n == null ? '—' : `${n}${suffix}`;
function notice(message = '', setupLink = false) {
  const banner = $('notice');
  banner.textContent = message;
  if (setupLink) {
    banner.textContent = 'Welcome. Open ';
    const link = document.createElement('a');
    link.href = '#settings'; link.textContent = 'Settings';
    link.onclick = event => { event.preventDefault(); tab('settings'); };
    banner.append(link, ' to connect your camera and start capturing.');
  }
  banner.classList.toggle('hidden', !message && !setupLink);
}
async function api(path, body) {
  const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', headers: body === undefined ? {} : {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: body === undefined ? undefined : JSON.stringify(body)});
  const value = await response.json();
  if (response.status === 401 && path !== '/api/login') { loggedOut(); throw new Error('Sign in to continue.'); }
  if (!response.ok) throw new Error(value.error || 'Request failed.');
  return value;
}
function loggedOut() { if (zoneFrameURL) { URL.revokeObjectURL(zoneFrameURL); zoneFrameURL = null; $('zone-frame').removeAttribute('src'); } zoneFrameReady = false; csrf = ''; $('app').classList.add('hidden'); $('login').classList.remove('hidden'); $('review-video').pause(); if ($('review').open) $('review').close(); }
function tab(name) { document.querySelectorAll('.tab').forEach(el => el.classList.toggle('hidden', el.id !== name)); document.querySelectorAll('.nav').forEach(el => el.classList.toggle('active', el.dataset.tab === name)); }
document.querySelectorAll('.nav').forEach(el => el.addEventListener('click', () => tab(el.dataset.tab)));
$('open-settings').onclick = () => tab('settings');
$('camera-settings-link').onclick = event => { event.preventDefault(); tab('settings'); }; $('view-all').onclick = () => tab('events');
async function signedIn() {
  const session = await api('/api/session'); csrf = session.csrf;
  $('login').classList.add('hidden'); $('app').classList.remove('hidden');
  settingsLoaded = false; clipSignature = ''; await refresh();
}
$('login-form').onsubmit = async event => { event.preventDefault(); $('login-error').textContent = ''; try { await api('/api/login', {username: $('login-user').value, password: $('login-password').value}); $('login-password').value = ''; await signedIn(); } catch (error) { $('login-error').textContent = error.message; } };
$('logout').onclick = async () => { try { await api('/api/logout', {}); } finally { loggedOut(); } };
function renderStatus(status) {
  const buffering = status.state === 'buffering';
  $('connection').textContent = buffering ? '● Recording' : status.state.replaceAll('_', ' ');
  $('connection').classList.toggle('warning', !buffering);
  $('analysis-state').textContent = `${status.analysis?.state || 'idle'}${status.analysis?.width ? ` · ${status.analysis.width}×${status.analysis.height} / ${number(status.analysis.fps, ' fps')}` : ''}`;
  $('camera-status').textContent = buffering ? 'Main stream buffering continuously' : `Camera: ${status.state.replaceAll('_', ' ')}`;
  $('trigger').disabled = !buffering;
  $('session-events').textContent = status.event_count_since_start || 0;
  $('buffer').textContent = number(status.buffer_span_seconds, ' sec');
  $('detection-mode').textContent = status.automatic_detection ? 'Tracking' : 'Manual';
  $('analysis-count').textContent = `${status.analysis?.frames || 0} frames · ${status.analysis?.dropped_frames || 0} dropped`;
  const h = status.health || {};
  $('cpu').textContent = number(h.cpu_percent, '%'); $('cpu-meter').value = h.cpu_percent || 0;
  $('memory').textContent = h.memory_used_bytes == null ? '—' : `${bytes(h.memory_used_bytes)} / ${bytes(h.memory_total_bytes)}`;
  $('memory-meter').value = h.memory_total_bytes ? h.memory_used_bytes / h.memory_total_bytes * 100 : 0;
  $('temperature').textContent = number(h.temperature_c, ' °C');
  $('disk').textContent = bytes(status.disk_free_bytes); $('used').textContent = bytes(status.storage_used_bytes);
  $('throttled').textContent = h.throttle_flags ?? 'Unavailable';
  $('uptime').textContent = `${Math.floor(status.uptime_seconds / 3600)}h ${Math.floor(status.uptime_seconds % 3600 / 60)}m`;
  $('version').textContent = `v${status.version}`;
  $('frame-age').textContent = status.frame_age_seconds == null ? 'NO FRAME' : `${Math.round(status.frame_age_seconds)}s AGO`;
  let connectionMessage = '';
  if (connectionStartedAt) {
    connectionMessage = buffering
      ? 'Camera connected. Recording is active. The live preview updates automatically; next, try Save an event.'
      : Date.now() - connectionStartedAt < 30000
        ? 'Settings saved. Connecting to your camera… The live preview will appear here automatically.'
        : 'Still waiting for the camera. Open Settings to check its address, RTSP login and stream paths; confirm RTSP is enabled on the camera.';
  }
  $('camera-empty-title').textContent = status.state === 'setup_required' ? 'Waiting for your camera' : buffering ? 'Waiting for camera preview' : 'Connecting to your camera…';
  notice(status.error || connectionMessage || (status.state === 'setup_required' ? 'Welcome. Open Settings to connect your camera and start capturing.' : '') || status.trigger_note || '', !status.error && status.state === 'setup_required');
}
function eventCard(event) {
  const card = document.createElement('article'); card.className = 'event-card';
  const image = document.createElement('div'); image.className = 'event-image';
  const base = `/api/clips/${encodeURIComponent(event.id)}`;
  if (event.thumbnail) { const thumb = document.createElement('img'); thumb.src = base + '/thumbnail'; thumb.alt = event.best_frame ? 'Native-resolution crop of the selected event frame' : 'Detection thumbnail for this event'; thumb.loading = 'lazy'; image.append(thumb); }
  if (event.thumbnail || event.best_frame || event.preview || event.substream_preview) { const view = document.createElement('button'); view.className = 'event-open'; view.textContent = 'View image'; view.setAttribute('aria-label', 'Open event image'); view.onclick = () => openReview(event); image.append(view); }
  const info = document.createElement('div'); info.className = 'event-info';
  const type = document.createElement('span'); type.className = 'type'; type.textContent = (event.trigger_type || 'event').replaceAll('_', ' ');
  const title = document.createElement('h3'); title.textContent = new Date(event.trigger_utc).toLocaleString();
  const detail = document.createElement('p'); detail.textContent = `${event.state}${event.estimated_duration_seconds ? ` · ${event.estimated_duration_seconds.toFixed(1)} sec` : ''}${event.interruption ? ' · interrupted' : ''}${event.substream_preview ? ' · substream video' : event.preview ? ' · detection replay' : ' · preview pending or unavailable'}${event.best_frame ? ' · native crop' : ''}`;
  const links = document.createElement('div'); links.className = 'event-links';
  for (const [label, kind] of [['Original ↓', 'original'], ['Full-res frame ↗', 'frame'], ['Object crop ↗', 'thumbnail'], ['Review details ↓', 'media'], ['Metadata ↓', 'metadata']]) { if (kind === 'original' && !event.original || kind === 'frame' && !event.best_frame || kind === 'thumbnail' && !event.best_frame || kind === 'media' && !event.best_frame) continue; const link = document.createElement('a'); link.href = base + '/' + kind; link.textContent = label; if (kind === 'thumbnail' || kind === 'frame') { link.onclick = click => { click.preventDefault(); openReview(event, kind); }; } links.append(link); }
  const identification = document.createElement('p'); identification.textContent = `Reference check: ${(event.identification || 'disabled').replaceAll('_', ' ')}`; info.append(type, title, detail, identification, links); card.append(image, info); return card;
}
function renderEvents(events) {
  if (reviewEvent && $('review').open) {
    const updated = events.find(event => event.id === reviewEvent.id);
    if (updated) { reviewEvent = updated; updateReviewAvailability(updated); if (reviewMode === 'image') showReviewImage(reviewImageKind); }
  }
  const signature = JSON.stringify(events); if (signature === clipSignature) return; clipSignature = signature;
  $('event-badge').textContent = events.length;
  for (const [id, list] of [['recent-events', events.slice(0, 3)], ['all-events', events]]) {
    const target = $(id); target.replaceChildren();
    if (!list.length) { const empty = document.createElement('div'); empty.className = 'empty'; const title = document.createElement('strong'); title.textContent = 'The sky has your attention.'; const text = document.createElement('span'); text.textContent = 'Events will appear here when motion qualifies, or when you save one manually.'; empty.append(title, text); target.append(empty); }
    else list.forEach(event => target.append(eventCard(event)));
  }
}
async function fillSettings() {
  const s = await api('/api/settings');
  for (const key of ['host', 'port', 'username', 'mainstream_path', 'substream_path']) $(key).value = s.camera[key] || '';
  for (const key of ['pre_roll_seconds', 'post_roll_seconds', 'max_event_seconds']) $(key).value = s.recording[key] ?? ({pre_roll_seconds:5, post_roll_seconds:5, max_event_seconds:120}[key]);
  $('camera-password').placeholder = s.camera.has_password ? 'Saved — leave blank to keep' : 'Enter the camera password';
  $('max_gb').value = s.storage.max_bytes / 1e9; $('free_gb').value = s.storage.min_free_bytes / 1e9; $('retention_days').value = s.storage.retention_days;
  for (const key of ['threshold', 'min_pixels', 'min_seconds', 'min_travel']) $(key).value = s.detection[key];
  $('enabled').checked = s.detection.enabled;
  ignoreZones = (s.detection.ignore_zones || []).map(zone => ({...zone}));
  renderZoneList(); drawZones(); loadZoneImage();
  const id = s.identification || {};
  for (const key of ['latitude','longitude','elevation_m','azimuth_deg','elevation_deg','roll_deg','horizontal_fov_deg','vertical_fov_deg','tolerance_deg','aircraft_feed_url','satellite_group','aircraft_provider']) $('id-'+key).value = id[key] ?? '';
  for (const key of ['enabled','calibrated','satellites']) $('id-'+key).checked = !!id[key];
  $('id-opensky_client_id').placeholder = id.has_opensky_credentials ? 'Saved — leave blank to keep' : 'Your OpenSky API client ID';
  settingsLoaded = true;
}
async function refresh() {
  if (!csrf || busy) return; busy = true;
  try { const [status, result] = await Promise.all([api('/api/status'), api('/api/clips')]); renderStatus(status); renderEvents(result.clips); if (!settingsLoaded) await fillSettings(); }
  catch (error) { if (csrf) { notice(`Station unavailable: ${error.message}`); $('connection').textContent = 'Disconnected'; $('connection').classList.add('warning'); $('trigger').disabled = true; } }
  finally { busy = false; }
}
let frameBusy = false;
async function refreshFrame() {
  if (!csrf || frameBusy || document.hidden) return; frameBusy = true;
  try { const response = await fetch('/api/frame.jpg'); if (!response.ok) { $('camera-frame').classList.add('hidden'); $('camera-empty').classList.remove('hidden'); return; } const url = URL.createObjectURL(await response.blob()); $('camera-frame').src = url; if (frameURL) URL.revokeObjectURL(frameURL); frameURL = url; $('camera-frame').classList.remove('hidden'); $('camera-empty').classList.add('hidden'); } catch { $('camera-frame').classList.add('hidden'); $('camera-empty').classList.remove('hidden'); } finally { frameBusy = false; }
}
$('settings-form').onsubmit = async event => { event.preventDefault(); const submit = event.submitter; submit.disabled = true; $('settings-result').textContent = 'Saving…'; const camera = {}; for (const key of ['host', 'username', 'mainstream_path', 'substream_path']) camera[key] = $(key).value.trim(); camera.port = Number($('port').value); camera.password = $('camera-password').value;
  const recording = {}; for (const key of ['pre_roll_seconds', 'post_roll_seconds', 'max_event_seconds']) recording[key] = Number($(key).value);
  const detection = {enabled: $('enabled').checked, ignore_zones: ignoreZones.map(zone => ({...zone}))}; for (const key of ['threshold', 'min_pixels', 'min_seconds', 'min_travel']) detection[key] = Number($(key).value);
  const identification = {};
  for(const key of ['latitude','longitude','elevation_m','azimuth_deg','elevation_deg','roll_deg','horizontal_fov_deg','vertical_fov_deg','tolerance_deg']) identification[key] = $('id-'+key).value === '' ? null : Number($('id-'+key).value);
  for(const key of ['enabled','calibrated','satellites']) identification[key] = $('id-'+key).checked;
  identification.aircraft_feed_url = $('id-aircraft_feed_url').value.trim(); identification.satellite_group = $('id-satellite_group').value; identification.aircraft_provider = $('id-aircraft_provider').value; identification.opensky_client_id = $('id-opensky_client_id').value; identification.opensky_client_secret = $('id-opensky_client_secret').value;
  try { await api('/api/settings', {camera, recording, detection, identification, storage: {max_bytes:Math.round(Number($('max_gb').value)*1e9), min_free_bytes:Math.round(Number($('free_gb').value)*1e9), retention_days:Number($('retention_days').value)}}); $('camera-password').value = ''; $('id-opensky_client_id').value = ''; $('id-opensky_client_secret').value = ''; $('settings-result').textContent = 'Saved. Reconnecting camera…'; settingsLoaded = false; connectionStartedAt = Date.now(); tab('overview');
    globalThis.scrollTo?.({top: 0, behavior: 'smooth'});
    notice('Settings saved. Connecting to your camera… The live preview will appear here automatically.');
    await refresh(); await refreshFrame(); } catch (error) { $('settings-result').textContent = error.message; } finally { submit.disabled = false; }
};
$('trigger').onclick = async () => { connectionStartedAt = 0; $('trigger').disabled = true; try { await api('/api/trigger', {}); notice('Event started. It will appear after post-roll and clip assembly.'); setTimeout(refresh, 1500); } catch(error) { notice(error.message); } };
$('reconnect').onclick = async () => { try { await api('/api/reconnect', {}); $('settings-result').textContent = 'Reconnecting…'; } catch(error) { $('settings-result').textContent = error.message; } };
$('refresh-events').onclick = refresh;
$('review-close').onclick = () => $('review').close(); $('review').addEventListener('close', () => { $('review-video').pause(); $('review-video').removeAttribute('src'); $('review-video').load(); reviewFrames = []; reviewMeta = null; reviewEvent = null; reviewMode = 'image'; $('review-still').removeAttribute('src'); drawMotion(); });
function drawZones() {
  const canvas = $('zone-canvas'), context = canvas.getContext('2d');
  context.clearRect(0, 0, canvas.width, canvas.height);
  for (const [index, zone] of [...ignoreZones, ...(zoneDraft ? [zoneDraft] : [])].entries()) {
    context.fillStyle = 'rgba(255,174,76,.28)'; context.strokeStyle = '#ffbf72'; context.lineWidth = 2;
    context.fillRect(zone.x * canvas.width, zone.y * canvas.height, zone.width * canvas.width, zone.height * canvas.height);
    context.strokeRect(zone.x * canvas.width, zone.y * canvas.height, zone.width * canvas.width, zone.height * canvas.height);
  }
}
function renderZoneList() {
  const list = $('zone-list'); list.replaceChildren();
  ignoreZones.forEach((zone, index) => {
    const row = document.createElement('div'); row.className = 'zone-row';
    const label = document.createElement('span');
    label.textContent = `Zone ${index + 1}: left ${(zone.x * 100).toFixed(1)}%, top ${(zone.y * 100).toFixed(1)}%, ${(zone.width * 100).toFixed(1)}% × ${(zone.height * 100).toFixed(1)}%`;
    const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'quiet'; remove.textContent = 'Remove';
    remove.setAttribute('aria-label', `Remove ignore zone ${index + 1}`);
    remove.onclick = () => { ignoreZones.splice(index, 1); renderZoneList(); drawZones(); $('zone-status').textContent = 'Zone removed. Save settings to apply.'; };
    row.append(label, remove); list.append(row);
  });
}
function addZone(zone) {
  if (ignoreZones.length >= 32) { $('zone-status').textContent = 'You can save up to 32 zones.'; return false; }
  if (!Object.values(zone).every(Number.isFinite) || zone.x < 0 || zone.y < 0 || zone.width <= 0 || zone.height <= 0 || zone.x + zone.width > 1 || zone.y + zone.height > 1) {
    $('zone-status').textContent = 'The rectangle must have a positive size and fit inside the image.'; return false;
  }
  ignoreZones.push(zone); renderZoneList(); drawZones();
  $('zone-status').textContent = `${ignoreZones.length} ignore zone(s). Save settings to apply.`; return true;
}
async function loadZoneImage() {
  try {
    const response = await fetch('/api/frame.jpg');
    if (!response.ok) throw new Error('No camera image');
    const url = URL.createObjectURL(await response.blob());
    $('zone-frame').src = url;
    if (zoneFrameURL) URL.revokeObjectURL(zoneFrameURL);
    zoneFrameURL = url;
  } catch { $('zone-status').textContent = 'No camera image yet. Connect the camera first, or enter rectangle coordinates below.'; }
}
$('zone-frame').onload = () => {
  const canvas = $('zone-canvas'), image = $('zone-frame');
  canvas.width = image.naturalWidth || 320; canvas.height = image.naturalHeight || 180;
  zoneFrameReady = true; drawZones();
  $('zone-status').textContent = 'Drag to exclude an area, or enter rectangle coordinates below. Save settings to apply.';
};
$('zone-refresh').onclick = loadZoneImage;
$('zone-add').onclick = () => addZone(Object.fromEntries(['x', 'y', 'width', 'height'].map(key => [key, Number($('zone-' + key).value) / 100])));
$('zone-clear').onclick = () => { ignoreZones = []; zoneDraft = null; zoneStart = null; renderZoneList(); drawZones(); $('zone-status').textContent = 'All zones cleared. Save settings to apply.'; };
function zonePoint(event) {
  const bounds = $('zone-canvas').getBoundingClientRect();
  if (!bounds.width || !bounds.height) return null;
  return {x: Math.min(1, Math.max(0, (event.clientX - bounds.left) / bounds.width)), y: Math.min(1, Math.max(0, (event.clientY - bounds.top) / bounds.height))};
}
function zoneRectangle(start, end) {
  const x = Math.min(start.x, end.x), y = Math.min(start.y, end.y);
  return {x, y, width: Math.min(Math.abs(start.x - end.x), 1 - x), height: Math.min(Math.abs(start.y - end.y), 1 - y)};
}
$('zone-canvas').onpointerdown = event => {
  if (!zoneFrameReady || (event.button !== undefined && event.button !== 0)) return;
  event.preventDefault(); zoneStart = zonePoint(event); zoneDraft = null;
  if (zoneStart) $('zone-canvas').setPointerCapture(event.pointerId);
};
$('zone-canvas').onpointermove = event => {
  if (!zoneStart) return; const point = zonePoint(event); if (!point) return;
  zoneDraft = zoneRectangle(zoneStart, point); drawZones();
};
$('zone-canvas').onpointerup = event => {
  if (!zoneStart) return; const point = zonePoint(event);
  const zone = point ? zoneRectangle(zoneStart, point) : null;
  zoneStart = null; zoneDraft = null;
  if (zone && zone.width >= .005 && zone.height >= .005) addZone(zone);
  drawZones();
};
$('zone-canvas').onpointercancel = () => { zoneStart = null; zoneDraft = null; drawZones(); };
setInterval(refresh, 2000); setInterval(refreshFrame, 1000);
signedIn().then(refreshFrame).catch(loggedOut);

let reviewFrames = [], reviewMeta = null, reviewEvent = null;
let reviewGeneration = 0, reviewMode = 'image', reviewImageKind = 'thumbnail';
async function openReview(event, imageKind = 'thumbnail') {
  const generation = ++reviewGeneration; reviewEvent = event; reviewFrames = []; reviewMeta = null;
  $('review-title').textContent = new Date(event.trigger_utc).toLocaleString();
  $('motion-info').textContent = event.motion ? 'Loading motion data…' : 'Motion data unavailable for this older event.';
  $('identification-detail').textContent = ''; $('identification-status').textContent = 'Loading reference check…';
  updateReviewAvailability(event);
  $('review-quality').value = event.substream_preview ? 'substream' : 'analysis';
  showReviewImage(imageKind);
  $('review').showModal(); drawMotion();
  loadIdentification(event.id);
  if(event.motion) {
    try {
      const response = await fetch(`/api/clips/${encodeURIComponent(event.id)}/motion`);
      if(!response.ok) throw new Error('Motion data unavailable');
      const records = (await response.text()).trim().split('\n').map(line=>JSON.parse(line));
      if(generation !== reviewGeneration || !reviewEvent || reviewEvent.id !== event.id) return;
      reviewMeta = records.shift();
      if(reviewMeta.kind !== 'magi_motion_v1' || reviewMeta.width > 640 || reviewMeta.height > 360) throw new Error('Unsupported motion data');
      reviewFrames = records; $('motion-info').textContent = `${records.length} sampled motion frames`; drawMotion();
    } catch { if(generation===reviewGeneration) $('motion-info').textContent = 'Motion data could not be loaded.'; }
  }
}
function drawMotion() {
  const canvas = $('motion-overlay'); const context = canvas.getContext('2d');
  context.clearRect(0,0,canvas.width,canvas.height);
  if(reviewMode !== 'video') { $('motion-info').textContent = 'Selected event image · Play clip starts at the beginning.'; return; }
  if($('review-quality').value !== 'analysis') { $('motion-info').textContent = 'Camera substream · choose Detection replay for pixel highlights.'; return; }
  if(!reviewMeta || !reviewFrames.length) return;
  canvas.width = reviewMeta.width; canvas.height = reviewMeta.height;
  const index = Math.min(reviewFrames.length-1, Math.max(0, Math.floor(($('review-video').currentTime||0)*reviewMeta.fps)));
  const sample = reviewFrames[index];
  if($('show-pixels').checked) {
    const packed = atob(sample.mask); const image = context.createImageData(canvas.width,canvas.height);
    for(let i=0;i<canvas.width*canvas.height;i++) if((packed.charCodeAt(i>>3)>>(7-(i&7)))&1) { const k=i*4; image.data[k]=174;image.data[k+1]=255;image.data[k+2]=105;image.data[k+3]=190; }
    context.putImageData(image,0,0);
  }
  if($('show-boxes').checked) {
    context.strokeStyle='#b5e9ff';context.lineWidth=1;
    for(const obj of sample.objects||[]) { const [x,y,x2,y2]=obj.box;context.strokeRect(x,y,x2-x,y2-y); }
  }
  $('motion-info').textContent = `Frame ${index+1}/${reviewFrames.length}${sample.suppressed ? ' · broad change suppressed by detector' : ''}`;
}
for(const event of ['timeupdate','seeked','loadedmetadata']) $('review-video').addEventListener(event,drawMotion);
$('show-pixels').onchange = drawMotion; $('show-boxes').onchange = drawMotion;
async function loadIdentification(id) {
  try { const report = await api(`/api/clips/${encodeURIComponent(id)}/identification`); if(reviewEvent?.id!==id) return;
    $('identification-status').textContent = report.status.replaceAll('_',' ');
    $('identification-detail').textContent = JSON.stringify(report, null, 2);
  } catch { if(reviewEvent?.id===id) $('identification-status').textContent = 'Not checked yet. Enable reference checks and configure camera geometry in Settings.'; }
}
$('identify-again').onclick = async () => {
  if(!reviewEvent) return; const id = reviewEvent.id;
  try { await api('/api/identify',{id}); $('identification-status').textContent='Check queued…'; setTimeout(()=>loadIdentification(id),2000); }
  catch(error) { $('identification-status').textContent=error.message; }
};

function updateReviewAvailability(event) {
  $('quality-substream').disabled = !event.substream_preview; $('quality-analysis').disabled = !event.preview;
  $('review-play').disabled = !(event.substream_preview || event.preview);
  $('review-full-frame').disabled = !event.best_frame;
  $('render-preview').disabled = !event.original || event.media_status === 'queued';
  const video = event.substream_preview ? 'Camera substream recording ready.' : 'Substream recording pending or unavailable; older events may only have detection replay.';
  const thumbnail = event.best_frame && event.media_status !== 'failed' ? 'Full-resolution thumbnail ready.' : event.media_status === 'failed' ? `Thumbnail unavailable: ${event.media_error || 'selection failed'}. You can retry.` : event.media_status === 'queued' ? 'Selecting a full-resolution thumbnail…' : 'Full-resolution thumbnail has not been generated yet.';
  $('render-status').textContent = `${video} ${thumbnail}`;
}
function showReviewImage(kind = 'thumbnail') {
  if (!reviewEvent) return;
  reviewMode = 'image'; reviewImageKind = kind;
  const video = $('review-video'); video.pause(); video.removeAttribute('src'); video.load();
  video.classList.add('hidden'); $('motion-overlay').classList.add('hidden');
  const available = kind === 'frame' ? reviewEvent.best_frame : reviewEvent.thumbnail;
  const still = $('review-still'); still.classList.toggle('hidden', !available);
  if (available) {
    const src = `/api/clips/${encodeURIComponent(reviewEvent.id)}/${kind === 'frame' ? 'frame' : 'thumbnail'}?selected=${reviewEvent.best_frame ? 1 : 0}`;
    if (still.getAttribute('src') !== src) still.src = src;
    still.alt = kind === 'frame' ? 'Selected event frame at full camera resolution' : reviewEvent.best_frame ? 'Full-resolution object crop from the selected event frame' : 'Temporary detection image; full-resolution selection is pending';
  } else still.removeAttribute('src');
  $('review-image-status').classList.toggle('hidden', available);
  $('review-image-status').textContent = 'The selected image is not available yet. You can play the clip while it is prepared.';
  $('show-pixels').disabled = true; $('show-boxes').disabled = true;
  drawMotion();
}
function selectReviewVideo() {
  if (!reviewEvent) return;
  reviewMode = 'video';
  $('review-still').classList.add('hidden'); $('review-image-status').classList.add('hidden');
  $('review-video').classList.remove('hidden'); $('motion-overlay').classList.remove('hidden');
  const analysis = $('review-quality').value === 'analysis';
  $('show-pixels').disabled = !analysis; $('show-boxes').disabled = !analysis;
  const video = $('review-video');
  video.src = `/api/clips/${encodeURIComponent(reviewEvent.id)}/${analysis ? 'preview' : 'video'}`;
  video.load(); video.currentTime = 0;
  drawMotion();
}
$('review-play').onclick = () => { selectReviewVideo(); $('review-video').play().catch(() => {}); };
$('review-photo').onclick = () => showReviewImage();
$('review-full-frame').onclick = () => showReviewImage('frame');
$('review-quality').onchange = () => { if (reviewMode === 'video') { selectReviewVideo(); $('review-video').play().catch(() => {}); } };
$('render-preview').onclick = async () => {
  if (!reviewEvent) return;
  $('render-preview').disabled = true;
  try { await api('/api/render', {id: reviewEvent.id}); $('render-status').textContent = 'Queued. The full-resolution thumbnail will update when frame selection finishes.'; }
  catch (error) { $('render-status').textContent = error.message; $('render-preview').disabled = false; }
};
