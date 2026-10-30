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
let reviewTracks=[], reviewColorTracks=[], reviewTimelineAttempted=false, trackBandSignature='';
let reviewGeneration = 0, reviewMode = 'image', reviewImageKind = 'thumbnail';
async function openReview(event, imageKind = 'thumbnail') {
  const generation = ++reviewGeneration; reviewEvent = event; reviewFrames = []; reviewMeta = null; reviewTracks=[]; reviewColorTracks=[]; reviewTimelineAttempted=false;
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
      if(reviewMeta.kind !== 'magi_motion_v1' || !Number.isInteger(reviewMeta.width) || !Number.isInteger(reviewMeta.height) || reviewMeta.width<=0 || reviewMeta.height<=0 || reviewMeta.width>1920 || reviewMeta.height>1080 || !Number.isFinite(reviewMeta.fps) || reviewMeta.fps<=0 || reviewMeta.fps>30) throw new Error('Unsupported motion data');
      reviewFrames = records; reviewTracks=MagiTracks.collect(records,reviewMeta); if(reviewEvent.substream_preview) await loadReviewTimeline(event.id,generation); if(generation!==reviewGeneration||reviewEvent?.id!==event.id) return; $('motion-info').textContent = `${records.length} sampled motion frames`; drawMotion();
    } catch { if(generation===reviewGeneration) { reviewMeta=null;reviewFrames=[];reviewTracks=[];reviewColorTracks=[];$('motion-info').textContent = 'Motion data could not be loaded.'; } }
  }
}
function currentTracks() {
  return $('review-quality').value === 'analysis' ? reviewTracks : reviewColorTracks;
}
function pathStroke(context, points, project, color, scale, dashed) {
  if(points.length<2) return;
  context.beginPath();
  points.forEach((point,index)=>{const [x,y]=project(point); if(index) context.lineTo(x,y); else context.moveTo(x,y);});
  context.strokeStyle='#07101b'; context.lineWidth=5*scale; context.setLineDash([]); context.stroke();
  context.strokeStyle=color; context.lineWidth=2*scale; context.setLineDash(dashed ? [5*scale,5*scale] : []); context.stroke(); context.setLineDash([]);
}
function trackMarker(context, x, y, color, scale) {
  const radius=13*scale;
  context.strokeStyle='#07101b'; context.lineWidth=5*scale; context.strokeRect(x-radius,y-radius,radius*2,radius*2);
  context.strokeStyle=color; context.lineWidth=2*scale; context.strokeRect(x-radius,y-radius,radius*2,radius*2);
}
function paintTracks(context, tracks, seconds, width, height) {
  const scale=Math.max(.5,width/850), project=point=>[point[1]*width,point[2]*height];
  tracks.forEach((track,index)=>{
    const other=track.id!==tracks[0].id, color=other ? '#7bcfff' : '#ff6b65', point=MagiTracks.position(track.points,seconds);
    pathStroke(context,track.points,project,other ? '#7bcfff' : '#f7c75e',scale,true);
    const completed=track.points.filter(p=>p[0]<=seconds);
    if(point && completed.length && completed.length<track.points.length) completed.push(point);
    pathStroke(context,completed,project,color,scale,false);
    if(point) {const [x,y]=project(point); trackMarker(context,x,y,color,scale);}
  });
}
function paintStillTracks(context,canvas) {
  const d=reviewEvent?.media_detail;
  if(!reviewEvent?.best_frame || !d || !Array.isArray(d.target) || d.target.length!==4 || !d.target.every(Number.isFinite)) return false;
  const crop=reviewImageKind==='frame' ? [0,0,d.native_width,d.native_height] : d.crop;
  if(!Array.isArray(crop)||crop.length!==4||!crop.every(Number.isFinite)||!(d.native_width>0&&d.native_height>0)||crop[2]<=crop[0]||crop[3]<=crop[1]||crop[2]-crop[0]>7680||crop[3]-crop[1]>4320) return false;
  canvas.width=crop[2]-crop[0]; canvas.height=crop[3]-crop[1];
  const scale=Math.max(.5,canvas.width/850), project=point=>[point[1]*d.native_width-crop[0],point[2]*d.native_height-crop[1]];
  if($('show-tracker').checked) {
    reviewTracks.forEach((track,index)=>{
      const other=track.id!==reviewTracks[0].id;
      pathStroke(context,track.points,project,other ? '#7bcfff' : '#f7c75e',scale,true);
      for(let i=0;i<track.points.length;i+=Math.max(1,Math.ceil(track.points.length/80))) {
        const [x,y]=project(track.points[i]);context.fillStyle=other ? '#7bcfff' : '#f7c75e';context.fillRect(x-scale*2,y-scale*2,scale*4,scale*4);
      }
      const [x,y]=project(track.points[track.points.length-1]);trackMarker(context,x,y,other ? '#7bcfff' : '#76e3a9',scale);
    });
    const [x,y]=project([0,d.target[0],d.target[1]]);trackMarker(context,x,y,'#ff6b65',scale);
  }
  return true;
}
function updateTrackNavigation() {
  const tracks=currentTracks(), video=$('review-video'), enabled=reviewMode==='video'&&tracks.length>0;
  const starts=tracks.map(t=>t.points[0][0]).sort((a,b)=>a-b);
  const t=video.currentTime||0;
  $('track-prev').disabled=!enabled||!starts.some(s=>s<t-.35);
  $('track-next').disabled=!enabled||!starts.some(s=>s>t+.35);
  $('track-timeline').disabled=!enabled;
  $('track-timeline').max=Number.isFinite(video.duration)&&video.duration>0 ? video.duration : Math.max(1,...tracks.map(x=>x.points[x.points.length-1][0]+.5));
  $('track-timeline').value=t;
  const duration=Number($('track-timeline').max), signature=JSON.stringify([reviewEvent?.id,$('review-quality').value,duration,tracks.map(track=>[track.id,track.points[0][0],track.points[track.points.length-1][0]])]);
  if(signature!==trackBandSignature) {
    trackBandSignature=signature;$('track-bands').replaceChildren();
    tracks.forEach(track=>{
      const band=document.createElement('span'), from=track.points[0][0], to=track.points[track.points.length-1][0];
      band.style.left=`${Math.max(0,Math.min(100,from/duration*100))}%`;
      band.style.width=`${Math.max(.5,Math.min(100-from/duration*100,(to-from)/duration*100))}%`;
      if(track.id!==tracks[0].id) band.className='companion';
      band.title=`Candidate interval ${from.toFixed(1)}–${to.toFixed(1)} seconds`;
      $('track-bands').append(band);
    });
  }
  $('track-times').textContent=enabled ? `${starts.length} tracked intervals · ${t.toFixed(1)} sec` : 'Tracking becomes available when a clip and saved track data are loaded.';
}
function drawMotion() {
  const canvas=$('motion-overlay'), context=canvas.getContext('2d');
  context.clearRect(0,0,canvas.width,canvas.height);
  updateTrackNavigation();
  if(reviewMode==='image') {
    const available=paintStillTracks(context,canvas);
    canvas.classList.toggle('hidden',!available);
    $('show-tracker').disabled=!available;
    $('motion-info').textContent=available ? 'Estimated positions: red marks the selected image target; amber shows the recorded path; green marks its last detection. Crop and timing alignment are approximate.' : 'Selected event image · Play clip starts at the beginning. No selected-image track position is available.';
    return;
  }
  const analysis=$('review-quality').value==='analysis', tracks=currentTracks();
  $('show-tracker').disabled=!tracks.length;
  canvas.width=reviewMeta?.width||640; canvas.height=reviewMeta?.height||360;
  let note=analysis ? 'Detection replay' : 'Camera substream · estimated track timing';
  if(analysis&&reviewMeta&&reviewFrames.length) {
    const index=Math.min(reviewFrames.length-1,Math.max(0,Math.floor(($('review-video').currentTime||0)*reviewMeta.fps)));
    const sample=reviewFrames[index];
    if($('show-pixels').checked) {
      try {
        const packed=atob(sample.mask);
        if(packed.length!==Math.ceil(canvas.width*canvas.height/8)) throw new Error('Incomplete mask');
        const image=context.createImageData(canvas.width,canvas.height);
        for(let i=0;i<canvas.width*canvas.height;i++) if((packed.charCodeAt(i>>3)>>(7-(i&7)))&1) {const k=i*4;image.data[k]=174;image.data[k+1]=255;image.data[k+2]=105;image.data[k+3]=190;}
        context.putImageData(image,0,0);
      } catch {note+=' · pixel mask unavailable';}
    }
    if($('show-boxes').checked) {
      context.strokeStyle='#b5e9ff';context.lineWidth=2;
      for(const obj of (sample.objects||[]).slice(0,32)) {const [x,y,x2,y2]=obj.box;context.strokeRect(x,y,x2-x,y2-y);}
    }
    note+=` · frame ${index+1}/${reviewFrames.length}${sample.suppressed?' · broad change rejected':''}`;
  }
  if($('show-tracker').checked) paintTracks(context,tracks,$('review-video').currentTime||0,canvas.width,canvas.height);
  $('motion-info').textContent=note+(tracks.length ? ' · candidates, not identifications' : ' · tracking unavailable; try Detection replay');
}
async function loadReviewTimeline(id,generation) {
  if(reviewTimelineAttempted) return;
  reviewTimelineAttempted=true;
  try {
    const timeline=await api(`/api/clips/${encodeURIComponent(id)}/timeline`);
    if(generation!==reviewGeneration||reviewEvent?.id!==id) return;
    if(!Array.isArray(timeline.segments)) throw new Error('Missing stream timeline');
    reviewColorTracks=MagiTracks.collect(reviewFrames,reviewMeta,timeline.segments);
    drawMotion();
  } catch { /* Keep exact analysis replay available when timing metadata is absent. */ }
}
function jumpTrack(direction) {
  if(reviewMode!=='video') return;
  const t=$('review-video').currentTime||0, starts=currentTracks().map(track=>track.points[0][0]).sort((a,b)=>a-b);
  const target=direction>0 ? starts.find(s=>s>t+.35) : starts.filter(s=>s<t-.35).pop();
  if(target===undefined) return;
  $('review-video').currentTime=Math.max(0,target-.25);drawMotion();$('review-video').play().catch(()=>{});
}
$('track-prev').onclick=()=>jumpTrack(-1);$('track-next').onclick=()=>jumpTrack(1);
$('track-timeline').oninput=()=>{if(reviewMode==='video') {$('review-video').currentTime=Number($('track-timeline').value);drawMotion();}};
$('show-tracker').onchange=drawMotion;
let trackAnimationActive=false;
$('review-video').addEventListener('play',()=>{
  if(trackAnimationActive||typeof requestAnimationFrame!=='function') return;
  trackAnimationActive=true;
  let last=0;
  function animate(now) {
    if(reviewMode!=='video'||$('review-video').paused||!$('review').open) {trackAnimationActive=false;return;}
    if(now-last>32) {drawMotion();last=now;}
    requestAnimationFrame(animate);
  }
  requestAnimationFrame(animate);
});
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
  if(reviewMeta&&event.substream_preview&&!reviewTimelineAttempted) loadReviewTimeline(event.id,reviewGeneration);
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
