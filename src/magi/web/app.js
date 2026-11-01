'use strict';
const $ = id => document.getElementById(id);
const selectedCamera = typeof location !== 'undefined' ? (new URL(location.href).searchParams.get('camera') || 'primary') : 'primary';
function cameraPath(path, identity = selectedCamera) {
  if (identity === 'primary' || !path.startsWith('/api/') || path.startsWith('/api/cameras/') || path.startsWith('/api/station/') || ['/api/login','/api/logout','/api/session','/api/cameras'].includes(path)) return path;
  return '/api/cameras/'+encodeURIComponent(identity)+'/'+path.slice(5);
}
const cameraTiles = new Map();
let stationCameras = [], cameraRefreshBusy = false, cameraAddBusy = false;
const eventKey = row => `${row.camera_id || selectedCamera}:${row.id}`;
const eventPath = (row, part) => `/api/cameras/${encodeURIComponent(row.camera_id || selectedCamera)}/clips/${encodeURIComponent(row.id)}/${part}`;
function cameraSettingsLink(camera, label, storage=false){
  const link=document.createElement('a');
  link.href='/?camera='+encodeURIComponent(camera.id)+(storage?'#storage-limits':'#settings');link.textContent=label;
  link.onclick=event=>{
    if(camera.id===selectedCamera && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey && !event.button){
      event.preventDefault();tab('settings');location.hash=storage?'storage-limits':'settings';if(storage)focusStorageLimits();refreshFrame();
    }
  };
  return link;
}
async function refreshCameras() {
  if (!csrf || cameraRefreshBusy) return;
  cameraRefreshBusy = true;
  try {
    const result = await api('/api/cameras');
    if (!csrf) return;
    stationCameras = result.cameras;
    if(result.cameras.length && !result.cameras.some(c=>c.id===selectedCamera) && !(result.archived_cameras || []).some(c=>c.id===selectedCamera)){
      location.href='/?camera='+encodeURIComponent(result.cameras[0].id)+'#overview';return;
    }
    const select = $('camera-select'), grid = $('camera-grid');
    const signature = JSON.stringify([result.cameras.map(c=>[c.id,c.name]),result.archived_cameras || []]);
    if (select.dataset.signature !== signature) {
      select.replaceChildren();$('camera-links').replaceChildren();
      const filter = $('event-camera'), previous = filter.value || 'all';filter.replaceChildren();
      const all=document.createElement('option');all.value='all';all.textContent='All cameras';filter.append(all);
      for (const camera of result.cameras) {
        const option=document.createElement('option');option.value=camera.id;option.textContent=camera.name;select.append(option);
        const choice=document.createElement('option');choice.value=camera.id;choice.textContent=camera.name;filter.append(choice);
        $('camera-links').append(cameraSettingsLink(camera,camera.name+' settings'));
      }
      for(const camera of result.archived_cameras || []){const choice=document.createElement('option');choice.value=camera.id;choice.textContent=camera.name+' (removed)';filter.append(choice);}
      filter.value=previous;select.value=selectedCamera;select.dataset.signature=signature;
      const current=result.cameras.find(c=>c.id===selectedCamera);
      if(current){$('camera-name').value=current.name;$('selected-camera-name').textContent=current.name;}
    }
    $('sidebar-camera-add').disabled=$('camera-add').disabled=cameraAddBusy || result.cameras.length >= result.max_cameras;
    $('sidebar-camera-add').title=result.cameras.length >= result.max_cameras?'Camera limit reached':'';
    const selectedActive=result.cameras.some(c=>c.id===selectedCamera);
    $('camera-remove').disabled=!selectedActive;
    $('camera-rename').disabled=$('camera-restart').disabled=result.max_cameras===1 || !selectedActive;
    for(const [id,tile] of cameraTiles){if(!result.cameras.some(c=>c.id===id)){if(tile.url)URL.revokeObjectURL(tile.url);tile.card.remove();cameraTiles.delete(id);}}
    for (const camera of result.cameras) {
      let tile=cameraTiles.get(camera.id);
      if(!tile){
        const card=document.createElement('article');card.className='panel camera-tile';card.dataset.cameraId=camera.id;
        const title=document.createElement('h2'), state=document.createElement('span'), count=document.createElement('small'), analysis=document.createElement('small');
        const img=document.createElement('img');img.alt=camera.name+' latest detection frame';img.classList.add('hidden');
        const empty=document.createElement('p');empty.textContent='Waiting for a fresh camera image.';
        const actions=document.createElement('div');actions.className='camera-tile-actions';
        const link=cameraSettingsLink(camera,'Settings');
        const save=document.createElement('button');save.type='button';save.className='secondary';save.textContent='＋ Save an event';
        const feedback=document.createElement('p');feedback.setAttribute('role','status');feedback.className='muted';
        tile={card,title,state,count,analysis,img,empty,save,feedback,url:null,pendingEvent:null,saving:false};
        save.onclick=async()=>{tile.saving=true;save.disabled=true;try{const result=await api('/api/trigger',{},camera.id);tile.pendingEvent=result.event_id;tile.eventStatusFailures=0;feedback.textContent='Event started. Waiting for post-roll and assembly.';}catch(error){feedback.textContent=error.message;}finally{tile.saving=false;save.disabled=!!tile.pendingEvent;}};
        actions.append(save,link);card.append(title,state,count,analysis,img,empty,actions,feedback);grid.append(card);cameraTiles.set(camera.id,tile);
      }
      tile.title.textContent=camera.name;tile.img.alt=camera.name+' latest detection frame';
      tile.state.textContent=`Main: ${camera.state.replaceAll('_',' ')} · Substream: ${(camera.substream?.state || 'idle').replaceAll('_',' ')} · Detection: ${(camera.analysis?.state || 'idle').replaceAll('_',' ')}`;
      tile.count.textContent=`${camera.event_count_since_start || 0} events this session · ${bytes(camera.storage_used_bytes)} used`;
      const a=camera.analysis || {};tile.analysis.textContent=`${a.width || '?'}×${a.height || '?'} @ ${a.fps || '?'} fps · ${a.frames || 0} detection frames · ${a.dropped_frames || 0} dropped`;
      tile.save.disabled=tile.saving || !!tile.pendingEvent || camera.state!=='buffering';
      if(tile.pendingEvent && !tile.saving){
        try {
          const event=await api('/api/clips/'+encodeURIComponent(tile.pendingEvent)+'/metadata',undefined,camera.id);
          tile.eventStatusFailures=0;
          const messages={collecting:'Saving event: collecting post-roll…',assembling:'Saving event: assembling recording…',complete:'Event saved. Available in Events.',interrupted:'Event saved with an interruption. Review it in Events.',failed:'Event could not be saved. Check Events and service status.'};
          tile.feedback.textContent=messages[event.state] || 'Checking event status…';
          if(['complete','interrupted','failed'].includes(event.state))tile.pendingEvent=null;
        }catch(error){tile.eventStatusFailures=(tile.eventStatusFailures || 0)+1;tile.feedback.textContent=tile.eventStatusFailures<3 ? 'Event status unavailable. Retrying…' : 'Could not confirm the event result. Check Events.';if(tile.eventStatusFailures>=3)tile.pendingEvent=null;}
      }
      tile.save.disabled=tile.saving || !!tile.pendingEvent || camera.state!=='buffering';
      tile.save.textContent=tile.saving || tile.pendingEvent ? 'Saving event…' : '＋ Save an event';
      if(camera.state==='worker_failed' || camera.frame_age_seconds==null || camera.frame_age_seconds>=10){tile.img.classList.add('hidden');tile.empty.classList.remove('hidden');}
    }
    const recording=result.cameras.filter(c=>c.state==='buffering').length;
    $('connection').textContent=`${recording}/${result.cameras.length} cameras recording`;$('connection').classList.toggle('warning',recording!==result.cameras.length);
    $('session-events').textContent=result.cameras.reduce((n,c)=>n+(c.event_count_since_start || 0),0);
    $('used').textContent=bytes(result.cameras.reduce((n,c)=>n+(c.storage_used_bytes || 0),0));
    $('buffer').textContent=`${recording} / ${result.cameras.length}`;
    $('detection-mode').textContent=`${result.cameras.filter(c=>c.analysis?.state==='analyzing').length} detecting`;
    $('analysis-count').textContent=`${result.cameras.reduce((n,c)=>n+(c.analysis?.frames || 0),0)} frames · ${result.cameras.reduce((n,c)=>n+(c.analysis?.dropped_frames || 0),0)} dropped`;
  } catch(error) { $('camera-management-status').textContent=error.message; }
  finally {cameraRefreshBusy=false;}
}
$('camera-select').onchange=()=>{
  const identity=$('camera-select').value;
  if(identity===selectedCamera){tab('settings');location.hash='settings';refreshFrame();}
  else location.href='/?camera='+encodeURIComponent(identity)+'#settings';
};
$('sidebar-camera-add').onclick=()=>{
  if($('sidebar-camera-add').disabled)return;
  $('camera-new-name').value='';$('camera-add-status').textContent='';$('camera-add-dialog').showModal();
};
$('camera-add-cancel').onclick=()=>{if(!cameraAddBusy)$('camera-add-dialog').close();};
$('camera-add-form').onsubmit=async event=>{
  event.preventDefault();if(cameraAddBusy)return;
  cameraAddBusy=true;
  $('sidebar-camera-add').disabled=$('camera-add').disabled=$('camera-add-cancel').disabled=true;
  $('camera-add-status').textContent='Adding camera…';
  try{
    const result=await api('/api/cameras',{action:'add',name:$('camera-new-name').value});
    $('camera-add-dialog').close();location.href='/?camera='+encodeURIComponent(result.id)+'#settings';
  }catch(error){$('camera-add-status').textContent=error.message;}
  finally{cameraAddBusy=false;$('camera-add-cancel').disabled=false;await refreshCameras();}
};
$('camera-rename').onclick=async()=>{
  $('camera-rename').disabled=true;
  try{await api('/api/cameras',{action:'rename',id:selectedCamera,name:$('camera-name').value});$('camera-management-status').textContent='Camera renamed.';await refreshCameras();}
  catch(error){$('camera-management-status').textContent=error.message;}
  finally{$('camera-rename').disabled=false;}
};
$('camera-restart').onclick=async()=>{
  $('camera-restart').disabled=true;
  try {await api('/api/cameras',{action:'restart',id:selectedCamera});$('camera-management-status').textContent='Camera worker restarted.';await refreshCameras();await refresh();}
  catch(error){$('camera-management-status').textContent=error.message;}
  finally{$('camera-restart').disabled=false;}
};
$('camera-remove').onclick=async()=>{
  const camera=stationCameras.find(c=>c.id===selectedCamera);
  if(!camera || !confirm('Remove '+camera.name+'? Capture will stop. Recordings and private archive configuration will be kept.'))return;
  $('camera-remove').disabled=true;
  try{
    await api('/api/cameras',{action:'remove',id:selectedCamera});
    const result=await api('/api/cameras');
    location.href='/?camera='+encodeURIComponent(result.cameras[0]?.id || 'primary')+'#overview';
  }catch(error){$('camera-management-status').textContent=error.message;$('camera-remove').disabled=false;}
};
let connectionStartedAt = 0;
let ignoreZones = [], zoneStart = null, zoneDraft = null, zoneFrameURL = null, zoneFrameReady = false;
let updateBusy=false, updateInProgress=false;
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
async function api(path, body, identity = selectedCamera) {
  const response = await fetch(cameraPath(path, identity), {method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', headers: body === undefined ? {} : {'Content-Type': 'application/json', 'X-CSRF-Token': csrf}, body: body === undefined ? undefined : JSON.stringify(body)});
  const value = await response.json();
  if (response.status === 401 && path !== '/api/login') { loggedOut(); throw new Error('Sign in to continue.'); }
  if (!response.ok) throw new Error(value.error || 'Request failed.');
  return value;
}
function loggedOut() { if(updateInProgress)$('login-error').textContent='Magi is updating or has restarted. Sign in again to see the update result.'; if(frameURL){URL.revokeObjectURL(frameURL);frameURL=null;$('camera-frame').removeAttribute('src');}for(const tile of cameraTiles.values()){if(tile.url)URL.revokeObjectURL(tile.url);}cameraTiles.clear();stationCameras=[];$('camera-grid').replaceChildren();MagiEvents.reset(); setupSignature=setupToken=null; setupError=''; for(const id of ['camera-password','setup-new-password','setup-confirm-password','id-opensky_client_id','id-opensky_client_secret']) $(id).value=''; if (zoneFrameURL) { URL.revokeObjectURL(zoneFrameURL); zoneFrameURL = null; $('zone-frame').removeAttribute('src'); } zoneFrameReady = false; csrf = ''; $('app').classList.add('hidden'); $('login').classList.remove('hidden'); $('review-video').pause(); if ($('review').open) $('review').close(); }
function tab(name) { document.querySelectorAll('.tab').forEach(el => el.classList.toggle('hidden', el.id !== name)); document.querySelectorAll('.nav').forEach(el => el.classList.toggle('active', el.dataset.tab === name)); MagiEvents.mount(name);if(name==='updates')refreshUpdates(true);if(name==='storage')refreshStorage();if(name==='events' || name==='overview')MagiEvents.refresh(true); }
document.querySelectorAll('.nav').forEach(el => el.addEventListener('click', () => tab(el.dataset.tab)));
$('open-settings').onclick = () => tab('settings');
$('camera-settings-link').onclick = event => { event.preventDefault(); tab('settings'); }; $('view-all').onclick = () => tab('events');
async function signedIn() {
  const session = await api('/api/session'); csrf = session.csrf;
  $('login').classList.add('hidden'); $('app').classList.remove('hidden');
  settingsLoaded = false; clipSignature = ''; await refreshCameras(); await refresh(); await refreshUpdates(true); if(typeof location !== 'undefined' && ['#settings','#storage-limits'].includes(new URL(location.href).hash)){tab('settings');if(location.hash==='#storage-limits')focusStorageLimits();}
}
$('login-form').onsubmit = async event => { event.preventDefault(); $('login-error').textContent = ''; try { await api('/api/login', {username: $('login-user').value, password: $('login-password').value}); $('login-password').value = ''; await signedIn(); } catch (error) { $('login-error').textContent = error.message; } };
$('opensky-credentials-file').onchange=async()=>{
  const input=$('opensky-credentials-file'), file=input.files?.[0];
  if(!file) return;
  try {
    if(file.size>16384) throw new Error('Credential file is too large. Choose the OpenSky credentials.json file.');
    const value=JSON.parse(await file.text());
    const id=value?.clientId ?? value?.client_id, secret=value?.clientSecret ?? value?.client_secret;
    if(!value || Array.isArray(value) || typeof id!=='string' || typeof secret!=='string' || !id.trim() || !secret.trim() || id.length>512 || secret.length>512) throw new Error('Expected clientId and clientSecret in the OpenSky credential file.');
    $('id-opensky_client_id').value=id.trim();$('id-opensky_client_secret').value=secret.trim();$('id-aircraft_provider').value='opensky';
    $('opensky-import-status').textContent='Credentials loaded locally. Save Settings to apply. Reference checks also need your location and calibrated camera pointing.';
  } catch(error) { $('opensky-import-status').textContent=error.name==='SyntaxError' ? 'This is not a valid JSON credential file.' : error.message; }
  finally {input.value='';}
};
$('logout').onclick = async () => { try { await api('/api/logout', {}); } finally { loggedOut(); } };
function renderUpdate(status){
  updateInProgress=!!status.busy || ['checking','downloading','installing'].includes(status.state);
  $('update-current').textContent=status.current_version ? 'v'+status.current_version:'Unavailable';
  $('update-available').textContent=status.available_version ? status.available_version+(status.preview?' (preview)':''):status.state==='current'?'Up to date':'Not available';
  $('update-install').dataset.version=status.available_version || '';
  $('update-status').textContent=status.message || 'Update status unavailable.';
  $('update-check').disabled=!status.helper_available || updateInProgress;
  $('update-install').disabled=!status.helper_available || updateInProgress || status.state!=='available';
  $('update-install').textContent=status.available_version?'Install '+status.available_version:'Install update';
  $('update-progress').classList.toggle('hidden',!updateInProgress);
  $('update-reload').classList.toggle('hidden',status.state!=='succeeded');
}
async function refreshUpdates(force=false){
  if(!csrf || updateBusy || (!force && !updateInProgress && $('updates').classList.contains('hidden')))return;
  updateBusy=true;
  try{renderUpdate(await api('/api/station/update'));}
  catch(error){$('update-status').textContent=updateInProgress?'Waiting for Magi to restart. Sign in again when it is available.':error.message;}
  finally{updateBusy=false;}
}
async function updateAction(action){
  if(updateBusy)return;updateBusy=true;
  $('update-check').disabled=$('update-install').disabled=true;
  try{renderUpdate(await api('/api/station/update',{action,...(action==='install'?{version:$('update-install').dataset.version}:{})}));}
  catch(error){$('update-status').textContent=error.message;}
  finally{updateBusy=false;}
}
$('update-check').onclick=()=>updateAction('check');
$('update-install').onclick=()=>updateAction('install');
$('update-reload').onclick=()=>location.reload();
setInterval(refreshUpdates,3000);
let latestSensors = null;
function renderSensors(data) {
  if(!data || !$('sensor-status')) return;
  latestSensors=data;
  const gps=data.gnss || {}, clock=data.clock || {}, aircraft=data.aircraft || {}, loc=data.effective_location || {};
  $('sensor-status').textContent='GPS: '+(data.station_location?.movement_detected ? 'station moved — reset location and recalibrate' : (gps.state || 'unavailable').replaceAll('_',' '))+(gps.utc?' · Fix UTC '+gps.utc:'')+(data.age_seconds!=null?' · Status age '+data.age_seconds+'s':'');
  $('sensor-location').textContent='Location: '+(loc.location_effective_source || 'unknown').replaceAll('_',' ')+' · '+(loc.latitude ?? '—')+', '+(loc.longitude ?? '—')+(gps.altitude_msl_m!=null?' · GPS altitude '+gps.altitude_msl_m+' m above mean sea level':'')+' · WGS84 elevation: '+(loc.altitude_effective_source || 'manual').replaceAll('_',' ')+(data.nmea && data.nmea.state!=='not_configured'?' · NMEA: '+data.nmea.state.replaceAll('_',' '):'');
  $('sensor-clock').textContent='Pi clock: '+(clock.state || 'unknown')+' · Source: '+(clock.source || 'unknown')+' · Serial GPS time is coarse, not PPS precision';
  $('sensor-aircraft').textContent='Local aircraft: '+(aircraft.state || 'unavailable').replaceAll('_',' ')+' · '+(aircraft.aircraft ?? 0)+' tracked · '+(aircraft.usable_positions ?? 0)+' usable geometric positions'+(aircraft.age_seconds!=null?' · Feed age '+aircraft.age_seconds+'s':'');
  if(settingsLoaded && $('id-location_source').value==='gnss') {
    for(const key of ['latitude','longitude']) { $('id-'+key).value=loc[key] ?? ''; if($('star-'+key).value==='' && loc[key]!=null) $('star-'+key).value=loc[key]; }
    if(loc.altitude_effective_source==='gnss_gga_wgs84'){ $('id-elevation_m').value=loc.elevation_m; $('star-elevation').value=loc.elevation_m; }
    $('id-elevation_m').readOnly=loc.altitude_effective_source==='gnss_gga_wgs84';
  }
}
function renderStatus(status) {
  renderSensors(status.sensors);
  // Camera settings diagnostics only. Station indicators are rendered solely
  // from /api/cameras in refreshCameras, including while that request is pending.
  renderCameraSetup(status);
  renderCameraInfo(status);
  const buffering = status.state === 'buffering';
  const analysis = status.analysis || {}, probeFailed = analysis.state === 'substream_probe_failed';
  const analysisNames = {starting:'Checking substream', connecting:'Connecting to substream', analyzing:'Detecting motion', reconnecting:'Reconnecting to substream', idle:'Detection idle'};
  $('analysis-state').textContent = probeFailed ? 'Motion detection paused' : `${analysisNames[analysis.state] || (analysis.state || 'idle').replaceAll('_', ' ')}${analysis.width ? ` · ${analysis.width}×${analysis.height} / ${number(analysis.fps, ' fps')}` : ''}`;
  $('analysis-state').classList.toggle('warning', probeFailed);
  $('analysis-diagnostic').textContent = probeFailed ? `${analysis.probe_error || 'Could not read the camera substream. Check the camera connection and substream settings.'} Resolution unknown. Retrying automatically${analysis.retry_in_seconds != null ? ` in ${analysis.retry_in_seconds} seconds` : ''}. ${buffering ? 'Main stream recording continues.' : 'See camera recording status below.'}` : '';
  $('analysis-diagnostic').classList.toggle('hidden', !probeFailed);
  $('camera-status').textContent = buffering ? 'Main stream buffering continuously' : `Camera: ${status.state.replaceAll('_', ' ')}`;
  $('trigger').disabled = !buffering;
  $('frame-age').textContent = status.frame_age_seconds == null ? 'NO FRAME' : `${Math.round(status.frame_age_seconds)}s AGO`;
  let connectionMessage = '';
  if (buffering) connectionStartedAt = 0;
  if (connectionStartedAt) {
    connectionMessage = Date.now() - connectionStartedAt < 30000
        ? 'Settings saved. Connecting to your camera… The live preview will appear here automatically.'
        : 'Still waiting for the camera. Open Settings to check its address, RTSP login and stream paths; confirm RTSP is enabled on the camera.';
  }
  $('camera-empty-title').textContent = status.state === 'setup_required' ? 'Waiting for your camera' : buffering ? 'Waiting for camera preview' : 'Connecting to your camera…';
  notice(status.error || connectionMessage || (status.state === 'setup_required' ? 'Welcome. Open Settings to connect your camera and start capturing.' : '') || status.trigger_note || '', !status.error && status.state === 'setup_required');
}
function renderHealth(status) {
  const h = status.health || {};
  $('cpu').textContent = number(h.cpu_percent, '%'); $('cpu-meter').value = h.cpu_percent || 0;
  $('memory').textContent = h.memory_used_bytes == null ? '—' : `${bytes(h.memory_used_bytes)} / ${bytes(h.memory_total_bytes)}`;
  $('memory-meter').value = h.memory_total_bytes ? h.memory_used_bytes / h.memory_total_bytes * 100 : 0;
  $('temperature').textContent = number(h.temperature_c, ' °C');
  $('disk').textContent = bytes(status.disk_free_bytes);
  $('throttled').textContent = h.throttle_flags ?? 'Unavailable';
  $('uptime').textContent = `${Math.floor((status.uptime_seconds || 0) / 3600)}h ${Math.floor((status.uptime_seconds || 0) % 3600 / 60)}m`;
  $('version').textContent = `v${status.version}`;
}
function renderEvents(events) {
  if (reviewEvent && $('review').open) {
    const updated = events.find(event => eventKey(event) === eventKey(reviewEvent));
    if (updated) { reviewEvent = updated; updateReviewAvailability(updated); if (reviewMode === 'image') showReviewImage(reviewImageKind); }
  }
  const signature = JSON.stringify(events); if (signature === clipSignature) return; clipSignature = signature;
  $('event-badge').textContent = events.length;

}
let setupSignature=null, setupToken=null, lastSetupState='', setupError='';
function setupFields(){return {host:$('host').value.trim(),username:$('username').value.trim() || 'admin',password:$('camera-password').value,initialize:$('setup-initialize').checked,new_password:$('setup-new-password').value,confirmation:$('setup-confirm-password').value};}
function renderCameraSetup(status){
  const s=status.camera_setup || {}, working=s.state==='working' || s.state==='saving';
  $('setup-status').textContent=setupError || (s.message || 'Not inspected.')+(s.retry_in_seconds>0 && !working ? ` Ready for another operation in ${s.retry_in_seconds}s.`:'');
  for(const id of ['setup-discover','setup-inspect']) $(id).disabled=working || s.retry_in_seconds>0;
  setupToken=s.state==='review' && setupSignature===JSON.stringify(setupFields()) ? s.token:null;
  $('setup-apply').disabled=!!setupError || !setupToken || working || s.retry_in_seconds>0;
  $('setup-apply').textContent=s.initialize ? 'Create password, enable services & connect' : 'Enable services & connect';
  const review=$('setup-review');review.replaceChildren();
  if(s.identity){const p=document.createElement('p');p.textContent=`Selected camera: ${s.host} · ${s.identity.model} · ${s.identity.name || 'Unnamed'} · hardware ${s.identity.hardware || 'unknown'} · firmware ${s.identity.firmware || 'unknown'}`;review.append(p);for(const [kind,port] of Object.entries(s.ports||{})){const line=document.createElement('p');line.textContent=`${kind.toUpperCase()}: port ${port.port}, ${port.enabled?'enabled':'disabled; will enable'}`;review.append(line);}}
  if(s.devices && $('setup-devices').dataset.signature!==JSON.stringify(s.devices)) {const select=$('setup-devices');select.replaceChildren();const empty=document.createElement('option');empty.value='';empty.textContent='Select a discovered camera';select.append(empty);for(const d of s.devices){const option=document.createElement('option');option.value=d.host;option.textContent=`${d.host} · ${d.label}`;select.append(option);}select.dataset.signature=JSON.stringify(s.devices);}
  if(s.state==='complete' && lastSetupState!=='complete') {settingsLoaded=false;$('camera-password').value='';$('setup-new-password').value='';$('setup-confirm-password').value='';setupSignature=setupToken=null;connectionStartedAt=Date.now();}
  lastSetupState=s.state;
}
async function runCameraSetup(action){
  try {setupError='';let data={action};if(action==='discover') data.network=$('setup-network').value.trim();
    if(action==='inspect'){const fields=setupFields();if(fields.initialize && !fields.new_password) throw new Error('Choose and confirm a new camera admin password before inspection. Apply will create it on the camera.');if(fields.initialize && fields.new_password!==fields.confirmation) throw new Error('The new camera passwords do not match.');setupSignature=JSON.stringify(fields);data={action,...fields};delete data.confirmation;}
    if(action==='apply'){if(!setupToken || setupSignature!==JSON.stringify(setupFields())) throw new Error('Inspect these connection details before applying setup.');data.token=setupToken;setupToken=null;}
    for(const id of ['setup-discover','setup-inspect','setup-apply']) $(id).disabled=true;
    const result=await api('/api/camera/setup',data);renderCameraSetup({camera_setup:result});
  }catch(error){setupError=error.message;setupSignature=setupToken=null;$('setup-status').textContent=setupError;for(const id of ['setup-discover','setup-inspect']) $(id).disabled=false;$('setup-apply').disabled=true;}
}
$('setup-discover').onclick=()=>runCameraSetup('discover');$('setup-inspect').onclick=()=>runCameraSetup('inspect');$('setup-apply').onclick=()=>runCameraSetup('apply');
$('setup-devices').onchange=()=>{if($('setup-devices').value){setupError='';$('host').value=$('setup-devices').value;updateCameraWebLink();setupToken=null;$('setup-apply').disabled=true;}};
for(const id of ['host','username','camera-password','setup-initialize','setup-new-password','setup-confirm-password']) $(id).addEventListener('input',()=>{setupError='';setupToken=null;$('setup-apply').disabled=true;});

async function refresh(){await refreshDashboard();await MagiEvents.refresh();}
function updateCameraWebLink() {
  const host=$('host').value.trim(), scheme=$('api_scheme').value, port=Number($('api_port').value), link=$('camera-web');
  link.classList.add('hidden'); link.removeAttribute('href');
  if(!host || /[\\/@?#\s]/.test(host) || !['http','https'].includes(scheme) || !Number.isInteger(port) || port<1 || port>65535) return;
  try {const url=new URL(`${scheme}://${host.includes(':') ? `[${host}]` : host}:${port}/`); link.href=url.href;link.classList.remove('hidden');} catch(_) {}
}
$('api_scheme').onchange=()=>{if(['80','443',''].includes(String($('api_port').value))) $('api_port').value=$('api_scheme').value==='https'?'443':'80';updateCameraWebLink();};
$('api_port').onchange=updateCameraWebLink; $('host').onchange=updateCameraWebLink;
function renderCameraInfo(status) {
  const info=status.camera_info || {}, analysis=status.analysis || {};
  $('camera-info-status').textContent=`${info.message || 'Not checked.'}${info.checked_at ? ` Checked ${new Date(info.checked_at*1000).toLocaleString()}.` : ''}${info.retry_in_seconds>0 && info.state!=='loading' ? ` Available to refresh in ${info.retry_in_seconds}s.` : ''}`;
  $('camera-info-read').disabled=info.state==='loading' || info.retry_in_seconds>0;
  const target=$('camera-info-details'); target.replaceChildren();
  const row=(label,value)=>{const p=document.createElement('p');const strong=document.createElement('strong');strong.textContent=label+': ';p.append(strong,value);target.append(p);};
  if(info.device) for(const [key,label] of [['model','Model'],['name','Camera name'],['hardware','Hardware'],['firmware','Firmware']]) row(label,info.device[key] || 'Not reported');
  if(info.streams) for(const [key,label] of [['main','Configured main stream'],['sub','Configured substream']]) {const s=info.streams[key] || {};row(label,`${s.width && s.height ? `${s.width}×${s.height}` : 'Resolution not reported'} · ${s.fps ?? 'Unknown'} fps · ${s.codec || 'Codec not reported'} · ${s.bitrate_kbps ?? 'Unknown'} kbps${s.path ? ` · Path: ${s.path}` : ''}`);}
  if(info.clock) {const c=info.clock;row('Camera local time',c.camera_local || 'Not reported');row('Pi UTC at check',c.pi_utc || 'Unknown');row('Clock difference',c.offset_seconds == null ? c.note : `${c.offset_seconds>0?'+':''}${c.offset_seconds}s (estimated uncertainty ±${c.uncertainty_seconds}s). ${c.note}`);}
  if(info.image) {const fields=Object.entries(info.image).filter(([,v])=>v!=null);row('Reported image settings',fields.length ? fields.map(([k,v])=>`${k}: ${v}`).join(' · ') : 'Not reported');}
  if(info.nominal_fov) row('Nominal model field of view',`${info.nominal_fov.horizontal_deg}° horizontal × ${info.nominal_fov.vertical_deg}° vertical. ${info.nominal_fov.note}`);
  if(info.unavailable_commands?.length) row('Unsupported or not permitted',info.unavailable_commands.join(', '));
  $('camera-info-observed').textContent=`Live status: main stream ${(status.state || 'unknown').replaceAll('_',' ')}; substream recording ${(status.substream?.state || 'unknown').replaceAll('_',' ')}. Detection ${analysis.width && analysis.state!=='substream_probe_failed' ? `last confirmed ${analysis.width}×${analysis.height} / ${analysis.fps} fps` : 'resolution unknown'}. Configured settings above do not prove video is arriving.`;
}
$('camera-info-read').onclick=async()=>{
  $('camera-info-read').disabled=true;$('camera-info-status').textContent='Reading saved camera connection…';
  try {const r=await api('/api/camera/info',{});renderCameraInfo({state:'unknown',camera_info:r.camera_info}); await refresh();}
  catch(error){$('camera-info-status').textContent=error.message;$('camera-info-read').disabled=false;}
};

async function fillSettings() {
  const s = await api('/api/settings');
  if (!$('setup-network').value.trim()) $('setup-network').value = s.camera_setup?.suggested_network || '';
  for (const key of ['host', 'port', 'username', 'mainstream_path', 'substream_path', 'api_scheme', 'api_port']) $(key).value = s.camera[key] || '';
  for (const key of ['pre_roll_seconds', 'post_roll_seconds', 'max_event_seconds']) $(key).value = s.recording[key] ?? ({pre_roll_seconds:5, post_roll_seconds:5, max_event_seconds:120}[key]);
  $('api_scheme').value = s.camera.api_scheme || 'http'; $('api_port').value = s.camera.api_port || 80;
  updateCameraWebLink();
  $('camera-password').placeholder = s.camera.has_password ? 'Saved — leave blank to keep' : 'Enter the camera password';
  $('max_gb').value = s.storage.max_bytes / 1e9; $('free_gb').value = s.storage.min_free_bytes / 1e9; $('retention_days').value = s.storage.retention_days; $('cleanup_gb').value = (s.storage.cleanup_margin_bytes || 0) / 1e9; $('trash_days').value = s.storage.trash_days ?? 7;
  for (const key of ['threshold', 'min_pixels', 'min_seconds', 'min_travel']) $(key).value = s.detection[key];
  $('enabled').checked = s.detection.enabled;
  for(const key of ['lightning_enabled','meteor_enabled','retain_aircraft_matches','retain_satellite_matches']) $(key).checked=s.detection[key] ?? true;
  for(const kind of ['motion','lightning_candidate','meteor_candidate']) $('retain-'+kind).checked=(s.detection.retain_types || ['motion','lightning_candidate','meteor_candidate']).includes(kind);
  ignoreZones = (s.detection.ignore_zones || []).map(zone => ({...zone}));
  renderZoneList(); drawZones(); loadZoneImage();
  const id = s.identification || {};
  for (const key of ['latitude','longitude','elevation_m','azimuth_deg','elevation_deg','roll_deg','horizontal_fov_deg','vertical_fov_deg','tolerance_deg','aircraft_feed_url','satellite_group','aircraft_provider','location_source']) $('id-'+key).value = id[key] ?? '';
  for (const field of ['star-latitude','star-longitude','star-elevation']) $(field).value='';
  if(s.star_calibration) $('star-status').textContent=`Saved star calibration · ${s.star_calibration.matches.length} stars · RMS ${s.star_calibration.fit.rms_deg.toFixed(3)}° · source event retained · recalibrate after moving the camera.`;
  for (const key of ['enabled','calibrated','satellites']) $('id-'+key).checked = !!id[key];
  $('id-opensky_client_id').placeholder = id.has_opensky_credentials ? 'Saved — leave blank to keep' : 'Your OpenSky API client ID';
  $('id-aircraft_local_file').checked=id.aircraft_local_file !== false;
  $('id-location_source').value=id.location_source || 'manual';
  setLocationMode();
  settingsLoaded = true;
  if(latestSensors)renderSensors(latestSensors);
}
async function refreshDashboard() {
  if (!csrf || busy) return; busy = true;
  try { const [status, result, health] = await Promise.allSettled([api('/api/status'), api('/api/station/clips'), api('/api/station/status')]); if(health.status==='fulfilled')renderHealth(health.value); if(status.status==='fulfilled')renderStatus(status.value);else {$('settings-result').textContent=status.reason.message;$('trigger').disabled=true;} if(result.status==='fulfilled')renderEvents(result.value.clips); await refreshCameras(); if (!settingsLoaded) {try {await fillSettings();} catch(error) {$('settings-result').textContent=error.message;}} }
  catch (error) { if (csrf) { notice(`Station unavailable: ${error.message}`); $('connection').textContent = 'Disconnected'; $('connection').classList.add('warning'); $('trigger').disabled = true; } }
  finally { busy = false; }
}
let frameBusy = false;
async function refreshFrame() {
  if (!csrf || frameBusy || document.hidden) return;
  frameBusy=true;const session=csrf;
  try {
    if(!$('overview').classList.contains('hidden')) await Promise.all(stationCameras.map(async camera=>{
      const tile=cameraTiles.get(camera.id);if(!tile)return;
      try {
        if(camera.state==='worker_failed')throw new Error('Unavailable');
        const response=await fetch(`/api/cameras/${encodeURIComponent(camera.id)}/frame.jpg`);
        if(!response.ok)throw new Error('No fresh frame');
        const blob=await response.blob();if(csrf!==session)return;
        const url=URL.createObjectURL(blob);tile.img.src=url;if(tile.url)URL.revokeObjectURL(tile.url);tile.url=url;
        tile.img.classList.remove('hidden');tile.empty.classList.add('hidden');
      }catch{tile.img.classList.add('hidden');tile.empty.classList.remove('hidden');}
    }));
    if(!$('settings').classList.contains('hidden')) {
      try {const response=await fetch(cameraPath('/api/frame.jpg'));if(!response.ok)throw new Error('No fresh frame');const blob=await response.blob();if(csrf!==session)return;const url=URL.createObjectURL(blob);$('camera-frame').src=url;if(frameURL)URL.revokeObjectURL(frameURL);frameURL=url;$('camera-frame').classList.remove('hidden');$('camera-empty').classList.add('hidden');}
      catch{$('camera-frame').classList.add('hidden');$('camera-empty').classList.remove('hidden');}
    }
  }finally{frameBusy=false;}
}
$('settings-form').onsubmit = async event => { event.preventDefault(); const submit = event.submitter; submit.disabled = true; $('settings-result').textContent = 'Saving…'; const camera = {}; for (const key of ['host', 'username', 'mainstream_path', 'substream_path']) camera[key] = $(key).value.trim(); camera.port = Number($('port').value); camera.api_scheme = $('api_scheme').value; camera.api_port = Number($('api_port').value); camera.password = $('camera-password').value;
  const recording = {}; for (const key of ['pre_roll_seconds', 'post_roll_seconds', 'max_event_seconds']) recording[key] = Number($(key).value);
  const detection = {enabled: $('enabled').checked, ignore_zones: ignoreZones.map(zone => ({...zone}))}; for (const key of ['threshold', 'min_pixels', 'min_seconds', 'min_travel']) detection[key] = Number($(key).value);
  for(const key of ['lightning_enabled','meteor_enabled','retain_aircraft_matches','retain_satellite_matches']) detection[key]=$(key).checked;
  detection.retain_types=['motion','lightning_candidate','meteor_candidate'].filter(kind=>$('retain-'+kind).checked);
  const identification = {};
  for(const key of ['latitude','longitude','elevation_m','azimuth_deg','elevation_deg','roll_deg','horizontal_fov_deg','vertical_fov_deg','tolerance_deg']) identification[key] = $('id-'+key).value === '' ? null : Number($('id-'+key).value);
  for(const key of ['enabled','calibrated','satellites']) identification[key] = $('id-'+key).checked;
  identification.location_source=$('id-location_source').value;
  identification.aircraft_local_file=$('id-aircraft_local_file').checked;
  if(identification.location_source==='gnss'){ delete identification.latitude; delete identification.longitude; if(latestSensors?.effective_location?.altitude_effective_source==='gnss_gga_wgs84')delete identification.elevation_m; }
  identification.aircraft_feed_url = $('id-aircraft_feed_url').value.trim(); identification.satellite_group = $('id-satellite_group').value; identification.aircraft_provider = $('id-aircraft_provider').value; identification.opensky_client_id = $('id-opensky_client_id').value; identification.opensky_client_secret = $('id-opensky_client_secret').value;
  try { await api('/api/settings', {camera, recording, detection, identification, storage: {max_bytes:Math.round(Number($('max_gb').value)*1e9), min_free_bytes:Math.round(Number($('free_gb').value)*1e9), retention_days:Number($('retention_days').value), cleanup_margin_bytes:Math.round(Number($('cleanup_gb').value)*1e9), trash_days:Number($('trash_days').value)}}); $('camera-password').value = ''; $('id-opensky_client_id').value = ''; $('id-opensky_client_secret').value = ''; $('opensky-import-status').textContent='Credentials saved locally on the Pi.'; $('settings-result').textContent = 'Saved. Reconnecting camera…'; settingsLoaded = false; connectionStartedAt = Date.now(); tab('overview');
    globalThis.scrollTo?.({top: 0, behavior: 'smooth'});
    notice('Settings saved. Connecting to your camera… The live preview will appear here automatically.');
    await refresh(); await refreshFrame(); } catch (error) { $('settings-result').textContent = error.message; } finally { submit.disabled = false; }
};
$('trigger').onclick = async () => { connectionStartedAt = 0; $('trigger').disabled = true; try { await api('/api/trigger', {}); notice('Event started. It will appear after post-roll and clip assembly.'); setTimeout(refresh, 1500); } catch(error) { notice(error.message); } };
$('reconnect').onclick = async () => { try { await api('/api/reconnect', {}); $('settings-result').textContent = 'Reconnecting…'; } catch(error) { $('settings-result').textContent = error.message; } };

$('review-close').onclick = () => $('review').close(); $('review').addEventListener('close', () => { $('review-video').pause(); $('review-video').removeAttribute('src'); $('review-video').load(); reviewGeneration++; reviewFrames = []; reviewMeta = null; reviewEvent = null; reviewMode = 'image'; $('review-still').removeAttribute('src'); drawMotion(); });
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
    const response = await fetch(cameraPath('/api/frame.jpg'));
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
  const generation = ++reviewGeneration; reviewEvent = event; MagiEvents.updateReview(event); reviewFrames = []; reviewMeta = null; reviewTracks=[]; reviewColorTracks=[]; reviewTimelineAttempted=false;
  $('review-title').textContent = `${event.camera_name || 'Camera 1'} · ${new Date(event.trigger_utc).toLocaleString()}`;
  $('motion-info').textContent = event.motion ? 'Loading motion data…' : 'Motion data unavailable for this older event.';
  $('radio-report').classList.add('hidden'); $('radio-status').textContent='';
  $('track-report').classList.add('hidden'); $('identification-detail').textContent = ''; $('identification-status').textContent = 'Loading reference check…';
  updateReviewAvailability(event);
  $('review-quality').value = event.substream_preview ? 'substream' : 'analysis';
  showReviewImage(imageKind);
  $('review').showModal(); drawMotion();
  loadIdentification(event.id);
  if(event.motion) {
    try {
      const response = await fetch(eventPath(event,'motion'));
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
    const timeline=await api(eventPath(reviewEvent,'timeline'));
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
  if(!reviewEvent || reviewEvent.id!==id)return;const key=eventKey(reviewEvent), generation=reviewGeneration;
  try { const report = await api(eventPath(reviewEvent,'identification')); if(generation!==reviewGeneration || !reviewEvent || eventKey(reviewEvent)!==key) return;
    $('identification-status').textContent = report.status.replaceAll('_',' ');
    $('track-report').href=eventPath(reviewEvent,'identification'); $('track-report').classList.remove('hidden');
    const radio=report.radio_evidence;
    $('radio-status').textContent=radio ? `Local radio: ${radio.aircraft_count || 0} aircraft recorded · ${radio.coverage} coverage · ${radio.association==='assessed'?'association assessed':'association not assessed'}. Nearby traffic alone does not identify the object.` : 'No saved radio evidence for this event.';
    $('radio-report').classList.toggle('hidden',!radio);if(radio)$('radio-report').href=eventPath(reviewEvent,'radio');
    const measurements=report.measurements;
    const summary=(measurements?.tracks || []).map(t=>`Track ${t.track_id}: ${t.sampled_path_angle_deg.toFixed(2)}° sampled path over ${t.duration_seconds.toFixed(2)}s · ${t.mean_path_speed_deg_s == null ? 'speed unavailable' : t.mean_path_speed_deg_s.toFixed(2)+'°/s mean angular speed'}`).join('\n');
    $('identification-detail').textContent = (summary ? summary+'\nHost timing and centroid uncertainty unverified; angles do not establish physical speed or distance.\n\n' : '')+JSON.stringify(report, null, 2);
  } catch { if(generation===reviewGeneration && reviewEvent && eventKey(reviewEvent)===key) $('identification-status').textContent = 'Not checked yet. Enable reference checks and configure camera geometry in Settings.'; }
}
$('identify-again').onclick = async () => {
  if(!reviewEvent) return; const event=reviewEvent, id=event.id, generation=reviewGeneration;
  try { await api('/api/identify',{id},event.camera_id || selectedCamera); if(generation!==reviewGeneration)return; $('identification-status').textContent='Check queued…'; setTimeout(()=>{if(generation===reviewGeneration)loadIdentification(id);},2000); }
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
  $('identify-again').disabled=event.state==='not_retained';
  $('render-status').textContent = event.state==='not_retained' ? 'Footage was excluded by retention rules. Only the event record and any reference report remain.' : `${video} ${thumbnail}`;
}
function showReviewImage(kind = 'thumbnail') {
  if (!reviewEvent) return;
  reviewMode = 'image'; reviewImageKind = kind;
  const video = $('review-video'); video.pause(); video.removeAttribute('src'); video.load();
  video.classList.add('hidden'); $('motion-overlay').classList.add('hidden');
  const available = kind === 'frame' ? reviewEvent.best_frame : reviewEvent.thumbnail;
  const still = $('review-still'); still.classList.toggle('hidden', !available);
  if (available) {
    const src = eventPath(reviewEvent,`${kind === 'frame' ? 'frame' : 'thumbnail'}?selected=${reviewEvent.best_frame ? 1 : 0}`);
    if (still.getAttribute('src') !== src) still.src = src;
    still.alt = kind === 'frame' ? 'Selected event frame at full camera resolution' : reviewEvent.best_frame ? 'Full-resolution object crop from the selected event frame' : 'Temporary detection image; full-resolution selection is pending';
  } else still.removeAttribute('src');
  $('review-image-status').classList.toggle('hidden', available);
  $('review-image-status').textContent = reviewEvent.state==='not_retained' ? 'No footage was retained for this event. You can still label and review its record.' : 'The selected image is not available yet. You can play the clip while it is prepared.';
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
  video.src = eventPath(reviewEvent,analysis ? 'preview' : 'video');
  video.load(); video.currentTime = 0;
  drawMotion();
}
$('review-play').onclick = () => { selectReviewVideo(); $('review-video').play().catch(() => {}); };
$('review-photo').onclick = () => showReviewImage();
$('review-full-frame').onclick = () => showReviewImage('frame');
$('review-quality').onchange = () => { if (reviewMode === 'video') { selectReviewVideo(); $('review-video').play().catch(() => {}); } };
$('render-preview').onclick = async () => {
  if (!reviewEvent) return;
  const event=reviewEvent,generation=reviewGeneration;
  $('render-preview').disabled = true;
  try { await api('/api/render', {id: event.id}, event.camera_id || selectedCamera);if(generation!==reviewGeneration)return; $('render-status').textContent = 'Queued. The full-resolution thumbnail will update when frame selection finishes.'; }
  catch (error) { $('render-status').textContent = error.message; $('render-preview').disabled = false; }
};
let starSession=null, starPoints=[], starSolution=null, starGeneration=0;
function invalidateStarFit() {starGeneration++;starSolution=null;$('star-apply').disabled=true;$('star-fit').textContent='';}
function renderStarPoints() {
  const list=$('star-points');list.replaceChildren();
  starPoints.forEach((point,index)=>{
    const row=document.createElement('div');row.className='star-point';
    const text=document.createElement('span');text.textContent=`${index+1}. ${point.star_id} · ${(point.x*100).toFixed(2)}%, ${(point.y*100).toFixed(2)}%`;
    const remove=document.createElement('button');remove.type='button';remove.className='quiet';remove.textContent='Remove';
    remove.onclick=()=>{starPoints.splice(index,1);invalidateStarFit();renderStarPoints();};row.append(text,remove);list.append(row);
  });
  drawStarPoints();
}
function drawStarPoints() {
  const canvas=$('star-canvas'),ctx=canvas.getContext('2d');ctx.clearRect(0,0,canvas.width,canvas.height);
  if(!starSession)return;
  canvas.width=starSession.width;canvas.height=starSession.height;
  const scale=Math.max(1,canvas.width/1000);
  starPoints.forEach((point,index)=>{const x=point.x*canvas.width,y=point.y*canvas.height;
    ctx.strokeStyle='#06101b';ctx.lineWidth=5*scale;ctx.strokeRect(x-7*scale,y-7*scale,14*scale,14*scale);
    ctx.strokeStyle='#76e3a9';ctx.lineWidth=2*scale;ctx.strokeRect(x-7*scale,y-7*scale,14*scale,14*scale);
    ctx.font=`${14*scale}px sans-serif`;ctx.fillStyle='#76e3a9';ctx.fillText(`${index+1} ${point.star_id}`,x+10*scale,y-10*scale);
    const expected=starSolution?.projected_matches?.[index];
    if(expected) {ctx.strokeStyle='#f7c75e';ctx.lineWidth=2*scale;ctx.strokeRect(expected[0]*canvas.width-4*scale,expected[1]*canvas.height-4*scale,8*scale,8*scale);}
  });
}
async function refreshStarFrames() {
  const result=await api('/api/clips'),select=$('star-event');select.replaceChildren();
  for(const event of result.clips.filter(event=>event.best_frame)) {
    const option=document.createElement('option');option.value=event.id;option.textContent=new Date(event.trigger_utc).toLocaleString();select.append(option);
  }
  if(select.children.length)select.value=select.children[0].value;
  $('star-status').textContent=select.children.length?'Choose a clear-night frame, then Load star frame.':'No full-resolution frames yet. Save an event and wait for its thumbnail selection.';
}
$('star-refresh').onclick=()=>openStarPicker(false).catch(error=>{$('star-status').textContent=error.message;});
async function loadManualStarFrame(){
  starSession=null;starPoints=[];invalidateStarFit();const generation=starGeneration;$('star-editor').classList.add('hidden');
  const location={latitude:$('star-latitude').value===''?null:Number($('star-latitude').value),longitude:$('star-longitude').value===''?null:Number($('star-longitude').value),elevation_m:$('star-elevation').value===''?null:Number($('star-elevation').value)};
  $('star-load').disabled=true;$('star-status').textContent='Computing visible named stars for the saved image time…';
  try {
    const result=await api('/api/calibration/load',{id:$('star-event').value,...location,use_gps:true});
    if(generation!==starGeneration)return;
    starSession=result;$('star-name').replaceChildren();
    for(const star of result.stars) {const option=document.createElement('option');option.value=star.id;option.textContent=`${star.name} · mag ${star.magnitude.toFixed(1)} · az ${star.azimuth_deg.toFixed(0)}° / alt ${star.altitude_deg.toFixed(0)}°`;$('star-name').append(option);}
    if(result.stars.length)$('star-name').value=result.stars[0].id;
    $('star-frame').src=cameraPath(`/api/clips/${encodeURIComponent(result.id)}/frame?calibration=${encodeURIComponent(result.token)}`);
    $('star-status').textContent=`Frozen frame: ${new Date(result.timestamp*1000).toLocaleString()} · ${result.width}×${result.height}. Choose a known star, then click its center. Identify any stars you recognize, or search without hints.`;
    $('star-editor').classList.remove('hidden');renderStarPoints();
  } catch(error){$('star-status').textContent=error.message;}
  finally{$('star-load').disabled=false;}
};
function addStarPoint(x,y) {
  if(!starSession||!Number.isFinite(x)||!Number.isFinite(y)||x<0||x>1||y<0||y>1)return;
  const name=$('star-name').value;
  if(!starSession.stars.some(star=>star.id===name))return;
  if(starPoints.length>=20||starPoints.some(point=>point.star_id===name)){$('star-status').textContent='Use each star once, up to 20. Remove a point to correct it.';return;}
  starPoints.push({star_id:name,x,y});invalidateStarFit();renderStarPoints();
  const next=starSession.stars.find(star=>!starPoints.some(point=>point.star_id===star.id));if(next)$('star-name').value=next.id;
}
$('star-canvas').onclick=event=>{const rect=$('star-canvas').getBoundingClientRect();addStarPoint((event.clientX-rect.left)/rect.width,(event.clientY-rect.top)/rect.height);};
$('star-add').onclick=()=>{if($('star-x').value!==''&&$('star-y').value!=='')addStarPoint(Number($('star-x').value)/100,Number($('star-y').value)/100);};
$('star-zoom').onchange=()=>{$('star-surface').style.width=`${Number($('star-zoom').value)*100}%`;};
$('star-clear').onclick=()=>{starPoints=[];invalidateStarFit();renderStarPoints();};
$('star-frame').addEventListener('load',drawStarPoints);
$('star-frame').addEventListener('error',()=>{$('star-status').textContent='The frozen frame could not be loaded. Choose the frame and load again.';invalidateStarFit();});
$('star-solve').onclick=async()=>{
  if(!starSession)return;
  $('star-solve').disabled=true;
  try{await api('/api/calibration/search',{token:starSession.token,matches:starPoints});autoStarRunning=true;await refreshAutoStars();$('star-status').textContent='Star search started with '+starPoints.length+' optional hints. Review the result in Automatic star search above.';}
  catch(error){$('star-status').textContent=error.message;}
  finally{$('star-solve').disabled=false;}
};
$('star-apply').onclick=async()=>{
  if(!starSession||!starSolution)return;
  $('star-apply').disabled=true;
  try{await api('/api/calibration/apply',{token:starSession.token});starSession=null;starSolution=null;$('star-editor').classList.add('hidden');await fillSettings();$('star-status').textContent='Star calibration saved. Recording is reconnecting; new events will include its provenance.';}
  catch(error){$('star-status').textContent=error.message;}
};

MagiEvents.init();

function setLocationMode(){
  const automatic=$('id-location_source').value==='gnss';
  for(const key of ['latitude','longitude']) $('id-'+key).readOnly=automatic;
  if(!automatic)$('id-elevation_m').readOnly=false;
}
$('id-location_source').onchange=()=>{setLocationMode();if(latestSensors)renderSensors(latestSensors);};

let storageBusy = false;
async function refreshStorage() {
  if(storageBusy) return;
  storageBusy=true; $('storage-refresh').disabled=true;
  $('storage-status').textContent='Measuring storage…';
  try {
    const result=await api('/api/station/storage');
    $('storage-cameras').replaceChildren();$('storage-volumes').replaceChildren();
    if(result.busy){$('storage-status').textContent='A storage scan is already running. Try again shortly.';return;}
    $('storage-status').textContent=`${result.partial?'Partial inventory — some usage is unavailable. ':''}Active cameras: ${bytes(result.active_bytes)} · Removed-camera archives: ${bytes(result.archived_bytes)}. Measured ${new Date(result.measured_at*1000).toLocaleTimeString()}. Results cached for 30 seconds.`;
    for(const volume of result.volumes){
      const line=document.createElement('p');
      line.textContent=`Disk: ${bytes(volume.free_bytes)} free of ${bytes(volume.total_bytes)} · ${volume.camera_ids.length} camera entries`;
      $('storage-volumes').append(line);
    }
    for(const camera of result.cameras){
      const card=document.createElement('article');card.className='panel form-panel';
      const title=document.createElement('h2');title.textContent=camera.name+(camera.archived?' (removed)':'');
      const info=document.createElement('p');
      info.textContent=camera.error || `${bytes(camera.used_bytes)} used · ${camera.archived?'Automatic cleanup disabled':`Cap ${bytes(camera.max_bytes)} · Free-space floor ${bytes(camera.min_free_bytes)} · Cleanup headroom ${bytes(camera.cleanup_margin_bytes)} · Retention ${camera.retention_days} days · Trash ${camera.trash_days} days`}`;
      card.append(title,info);
      if(camera.state==='storage_blocked'){const warning=document.createElement('p');warning.textContent='Capture paused: the camera cap or free-space floor cannot be satisfied. Check protected/in-use footage and other disk users.';card.append(warning);}
      if(!camera.archived)card.append(cameraSettingsLink(camera,'Edit storage limits',true));
      const actions=document.createElement('div');actions.className='update-actions';
      for(const [mode,label] of [['unprotected','Purge unprotected events'],['trash','Empty trash'],...(camera.archived?[['remove_archive','Delete archived camera']]:[])]){
        const button=document.createElement('button');button.type='button';button.className='secondary';button.textContent=label;button.onclick=()=>purgeStorage(camera.id,mode);actions.append(button);
      }
      card.append(actions);$('storage-cameras').append(card);
    }
  } catch(error){$('storage-status').textContent=error.message;}
  finally {storageBusy=false;$('storage-refresh').disabled=false;}
}
$('storage-refresh').onclick=refreshStorage;

function focusStorageLimits(){
  $('storage-limits').scrollIntoView({block:'start',behavior:'smooth'});
  $('max_gb').focus({preventScroll:true});
}
let purgeBusy=false;
async function purgeStorage(camera,mode){
  if(purgeBusy)return;
  purgeBusy=true;
  const status=$('storage-purge-status');status.textContent='Preparing deletion preview…';
  try {
    const preview=await api('/api/station/storage/purge',{action:'preview',camera_id:camera,mode});
    if(!preview.count && mode!=='remove_archive'){await api('/api/station/storage/purge',{action:'cancel',token:preview.token});status.textContent='No eligible events to purge.';return;}
    const message=`${preview.scope}: permanently delete ${preview.count} unprotected events (about ${bytes(preview.estimated_bytes)} of files)? This includes originals, previews, notes and metadata. ${preview.retained} protected/unfinished/unreadable entries retained. Events in use will be rechecked and skipped. ${mode==='remove_archive'?'The removed camera will also disappear from Storage and Events; its leftover buffers will be deleted. ':''}This cannot be undone. File size is not guaranteed reclaimed space.`;
    if(!confirm(message)){await api('/api/station/storage/purge',{action:'cancel',token:preview.token});status.textContent='Cancelled. No files deleted.';return;}
    let step=0,result;
    do {
      result=await api('/api/station/storage/purge',{action:'confirm',token:preview.token,step});
      step=result.step;
      status.textContent=`Deleted ${result.deleted} events; ${result.failed} skipped or failed. Working…`;
    } while(!result.done && csrf);
    status.textContent=`Deleted ${result.deleted} events. ${result.failed} skipped or failed; ${result.retained} retained.${result.removed?' Archived camera removed.':''}${result.error?' '+result.error:''}`;
    await refreshStorage();await refreshCameras();await MagiEvents.refresh(true);
  }catch(error){status.textContent='Deletion stopped: '+error.message+' Refresh before retrying; completed deletions are permanent.';}
  finally{purgeBusy=false;}
}
$('storage-purge-all').onclick=()=>purgeStorage('all','unprotected');
$('storage-empty-trash').onclick=()=>purgeStorage('all','trash');

// Automatic solving is isolated from capture and never applies without review.
let autoStarCandidate=null, autoStarBusy=false, autoStarRunning=false, autoStarImageReady=false;
const autoStarImage=document.createElement('img');
function autoStarApplyState(){
  $('auto-star-apply').disabled=autoStarBusy || autoStarRunning || !autoStarImageReady || !autoStarCandidate || autoStarCandidate.status!=='candidate' || !$('auto-star-reviewed').checked;
}
autoStarImage.onload=()=>{
  const canvas=$('auto-star-canvas'),ctx=canvas.getContext('2d');
  canvas.width=autoStarImage.naturalWidth;canvas.height=autoStarImage.naturalHeight;
  ctx.drawImage(autoStarImage,0,0);ctx.strokeStyle='#ffdc00';ctx.fillStyle='#ffdc00';ctx.lineWidth=3;ctx.font='18px sans-serif';
  for(const match of autoStarCandidate?.matches || []){
    const x=match.x*canvas.width,y=match.y*canvas.height;ctx.beginPath();ctx.arc(x,y,10,0,Math.PI*2);ctx.stroke();ctx.fillText(match.catalog_id,x+12,y);
  }
  autoStarImageReady=true;autoStarApplyState();
};
autoStarImage.onerror=()=>{autoStarImageReady=false;autoStarApplyState();$('auto-star-status').textContent='Candidate frame is unavailable. Choose another saved frame and solve again.';};
async function refreshAutoStars(){
  if(!csrf || autoStarBusy)return;
  autoStarBusy=true;autoStarApplyState();
  try{
    const data=await api('/api/auto-calibration/status',{}),candidate=data.candidate;
    autoStarRunning=!!data.job?.running;
    const gps=data.search_location || {};
    $('star-gps-status').textContent=gps.latitude!=null&&gps.longitude!=null&&gps.elevation_m!=null?'GPS WGS84 location · '+(gps.location_effective_source || 'GNSS'):'GPS position or validated WGS84 elevation unavailable. Check station sensors.';
    for(const [field,key] of [['star-latitude','latitude'],['star-longitude','longitude'],['star-elevation','elevation_m']]) $(field).value=data.search_location?.[key] ?? '';
    $('auto-star-cancel').disabled=!autoStarRunning;
    $('auto-star-start').disabled=autoStarRunning;
    const outcome=data.job?.error || data.job?.reason || data.status.reason || data.status.status;
    $('auto-star-status').textContent=(autoStarRunning?'Searching locally, up to two minutes; recording continues.'+(data.job.temperature_available?'':' Temperature reading unavailable; time/resource limits still apply.'):`Latest search: ${outcome || 'not run'}.`)+(data.status.movement_suspect?' Camera movement suspected: review and recalibrate.':'');
    if(candidate?.created_unix!==autoStarCandidate?.created_unix || candidate?.status!==autoStarCandidate?.status){
      autoStarCandidate=candidate;autoStarImageReady=false;$('auto-star-reviewed').checked=false;
      $('auto-star-review').classList.toggle('hidden',!candidate);
      if(candidate){
        const g=candidate.geometry,v=candidate.verification || {},r=candidate.residuals_deg || {};
        $('auto-star-fit').textContent=`Candidate from ${new Date(candidate.image_timestamp_unix*1000).toLocaleString()}\nAzimuth ${g.azimuth_deg.toFixed(3)}° · elevation ${g.elevation_deg.toFixed(3)}° · roll ${g.roll_deg.toFixed(3)}°\nField of view ${g.horizontal_fov_deg.toFixed(3)}° × ${g.vertical_fov_deg.toFixed(3)}°\n${v.matched_count || 0} matched stars · ${v.verification_count || 0} additional verification stars\nAll-star RMS ${r.all_rms_deg?.toFixed(3) ?? '?'}° · maximum ${r.all_max_deg?.toFixed(3) ?? '?'}°\nPinhole model; lens distortion is not fitted. Host time is not verified camera exposure time.\n${candidate.status==='applied'?'Already applied.':'This candidate remains unchanged if a later solve fails.'}`;
        autoStarImage.src=cameraPath(`/api/clips/${encodeURIComponent(candidate.source_event)}/frame?auto=${candidate.created_unix}`);
      }
    }
  }catch(error){$('auto-star-status').textContent=error.message;}
  finally{autoStarBusy=false;autoStarApplyState();}
}
$('auto-star-refresh').onclick=refreshAutoStars;
$('auto-star-reviewed').onchange=autoStarApplyState;
$('auto-star-start').onclick=async()=>{
  if(autoStarBusy)return;autoStarBusy=true;autoStarApplyState();
  try{const id=$('star-event').value;if(!id)throw new Error('Find saved frames and select a clear frame first.');await api('/api/auto-calibration/start',{id});autoStarRunning=true;}
  catch(error){$('auto-star-status').textContent=error.message;return;}
  finally{autoStarBusy=false;autoStarApplyState();}
  await refreshAutoStars();
};
$('auto-star-cancel').onclick=async()=>{try{await api('/api/auto-calibration/cancel',{});await refreshAutoStars();}catch(error){$('auto-star-status').textContent=error.message;}};
$('auto-star-apply').onclick=async()=>{
  if($('auto-star-apply').disabled)return;autoStarBusy=true;autoStarApplyState();
  try{await api('/api/auto-calibration/apply',{created_unix:autoStarCandidate.created_unix});await fillSettings();}
  catch(error){$('auto-star-status').textContent=error.message;return;}
  finally{autoStarBusy=false;autoStarApplyState();}
  await refreshAutoStars();
};
setInterval(()=>{if(autoStarRunning)refreshAutoStars();},3000);

$('star-load').onclick=()=>openStarPicker(true).catch(error=>{$('star-status').textContent=error.message;});
$('star-picker-close').onclick=()=>$('star-picker').close();
let starPickerGeneration=0;
async function openStarPicker(manual){
  const generation=++starPickerGeneration;
  await refreshStarFrames();await refreshAutoStars();
  const result=await api('/api/clips');
  if(generation!==starPickerGeneration)return;
  const grid=$('star-picker-grid');grid.replaceChildren();
  const rows=result.clips.filter(row=>row.best_frame).slice(0,24);
  $('star-picker-status').textContent=rows.length?'Recent frames; loading preview quality suggestions…':'No saved stills yet. Save an event and wait for its full-resolution frame.';
  $('star-picker').showModal();
  const cards=[];
  for(const row of rows){
    const card=document.createElement('button');card.type='button';card.className='secondary';
    const img=document.createElement('img');img.alt='Saved sky frame';img.style.width='100%';
    const caption=document.createElement('span');caption.textContent=new Date(row.trigger_utc).toLocaleString();
    const quality=document.createElement('small');quality.textContent='Preview quality unavailable';
    card.append(img,caption,quality);grid.append(card);cards.push({card,score:-1});const item=cards[cards.length-1];
    card.onclick=async()=>{$('star-event').value=row.id;$('star-picker').close();$('star-status').textContent='Selected '+caption.textContent;if(manual)await loadManualStarFrame();};
    img.onload=()=>{
      if(generation!==starPickerGeneration)return;
      try{
        const canvas=document.createElement('canvas');canvas.width=160;canvas.height=90;
        const ctx=canvas.getContext('2d');ctx.drawImage(img,0,0,160,90);const data=ctx.getImageData(0,0,160,90).data;
        const gray=Array.from({length:14400},(_,i)=>(data[i*4]+data[i*4+1]+data[i*4+2])/3);
        const mean=gray.reduce((a,b)=>a+b,0)/gray.length;let edges=0,points=0;
        for(let y=1;y<89;y++)for(let x=1;x<159;x++){const i=y*160+x;edges+=Math.abs(gray[i]-gray[i-1]);if(gray[i]>mean+35&&gray[i]>gray[i-1]&&gray[i]>gray[i+1]&&gray[i]>gray[i-160]&&gray[i]>gray[i+160])points++;}
        item.score=Math.min(points,100)+Math.min(edges/14000,20)-(mean>150?50:0);
        quality.textContent=points>=4&&mean<150?'Suggested preview: '+points+' star-like points':'Lower preview score; may still be usable';
        cards.sort((a,b)=>b.score-a.score);for(const c of cards)grid.append(c.card);
        $('star-picker-status').textContent='Showing up to 24 recent stills, suggested previews first. Thumbnail scores can miss faint stars or favor noise.';
      }catch(error){quality.textContent='Preview quality unavailable';}
    };
    img.src=cameraPath('/api/clips/'+encodeURIComponent(row.id)+'/thumbnail');
  }
}
