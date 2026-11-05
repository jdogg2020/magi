'use strict';
const MagiEvents = (()=>{
  const el=id=>document.getElementById(id), selected=new Map();
  let rows=[], total=0, offset=0, loaded=false, loading=false, lastRefresh=0, requestVersion=0, mutating=false, exporting=false, currentFilters=null;
  let neighbors={previous:null,next:null}, navigationVersion=0, reviewBusy=false;
  let navigationTask=null, navigationKey=null, navigationFilter=-1, navigationReady=false;
  const pendingSaves=new Map(), localReviews=new Map();
  let draining=false, advancingKey=null;
  function withLocalReview(row){
    const patch=row && localReviews.get(eventKey(row));
    return patch ? {...row,review:{...row.review,...patch}} : row;
  }
  function saveIndicators(){
    const failed=[...pendingSaves.values()].filter(job=>job.state==='failed');
    const count=pendingSaves.size-failed.length;
    for(const id of ['background-saves','review-background-saves']){
      const panel=el(id);panel.replaceChildren();panel.classList.toggle('hidden',!pendingSaves.size);
      if(!pendingSaves.size)continue;
      panel.append(node('p',`${count ? `Saving ${count} change${count===1?'':'s'}… ` : ''}${failed.length ? `${failed.length} save${failed.length===1?'':'s'} failed. Changes are still held in this tab.` : 'You can keep reviewing. Keep this tab open until saving finishes.'}`));
      for(const job of failed){
        const line=node('div');line.append(node('span',`${job.row.camera_name || job.row.camera_id} · ${job.row.id}: ${job.error} `));
        line.append(button('Review',()=>{if(mayLeave())openReview(withLocalReview(job.row));}));
        line.append(button('Retry',()=>{job.state='queued';saveIndicators();drainSaves();}));
        line.append(button('Discard unsaved changes',()=>{
          if(!confirm('Discard these unsaved review changes? The server may have received a timed-out request. Refresh to check its saved state.'))return;
          pendingSaves.delete(eventKey(job.row));localReviews.delete(eventKey(job.row));saveIndicators();reviewControls();refresh(true);
        }));panel.append(line);
      }
    }
    el('logout').disabled=!!pendingSaves.size;
    selection();
  }
  async function drainSaves(){
    if(draining)return;draining=true;
    try {
      for(let job; (job=[...pendingSaves.values()].find(item=>item.state==='queued'));){
        const key=eventKey(job.row), revision=job.revision, changes={...job.changes,labels:[...job.changes.labels]};
        job.state='saving';saveIndicators();
        try {
          const result=await api('/api/station/events/update',{targets:[{id:job.row.id,camera_id:job.row.camera_id}],action:'update',changes});
          if(result.failed?.length)throw new Error(result.failed[0].error);
          if(!result.updated?.some(row=>eventKey(row)===key))throw new Error('Save was not confirmed.');
          if(job.revision===revision)pendingSaves.delete(key);else job.state='queued';
          lastRefresh=0;clipSignature='';
        }catch(error){job.state='failed';job.error=error.message || 'Save failed. Sign in again if needed, then retry.';}
        saveIndicators();reviewControls();
      }
    } finally {draining=false;}
  }
  function labelReview(changes,row){
    const existing=new Set((row?.review?.labels || []).map(label=>label.trim().toLowerCase()));
    return changes.labels?.some(label=>!existing.has(label.trim().toLowerCase())) ? {...changes,reviewed:true} : changes;
  }
  async function queueReview(changes){
    if(!reviewEvent || reviewBusy || mutating || advancingKey===eventKey(reviewEvent))return;
    const event=withLocalReview(reviewEvent),key=eventKey(event);
    if(!pendingSaves.has(key) && pendingSaves.size>=100){el('review-save-status').textContent='100 changes are waiting. Let saves finish or retry failures before continuing.';return;}
    changes=labelReview(changes,event);
    const existing=pendingSaves.get(key);
    if(existing){existing.changes=changes;existing.revision++;if(existing.state==='failed')existing.state='queued';}
    else pendingSaves.set(key,{row:event,changes,revision:1,state:'queued'});
    localReviews.set(key,{...changes,labels:[...changes.labels]});
    // Bound confirmed local overlays without evicting any unsaved work.
    if(localReviews.size>1000)for(const old of localReviews.keys()){if(!pendingSaves.has(old)){localReviews.delete(old);break;}}
    reviewEvent=withLocalReview(event);updateReview(reviewEvent,false);
    lastRefresh=0;clipSignature='';advancingKey=key;saveIndicators();reviewControls();
    drainSaves();
    try {
      const found=navigationKey===key && navigationFilter===requestVersion && navigationTask
        ? (navigationReady || await navigationTask) : await loadNeighbors(event);
      if(!reviewEvent || eventKey(reviewEvent)!==key)return;
      if(found && neighbors.next)openReview(withLocalReview(neighbors.next));
      else el('review-save-status').textContent=found?'Last matching event — changes are saving in the background.':'Changes queued. Navigation unavailable; use the list to continue.';
    } finally {if(advancingKey===key)advancingKey=null;reviewControls();}
  }
  const quickLabels=[['ignore','Ignore'],['aircraft','Plane'],['bird/insect','Bird / insect'],['lightning','Lightning'],['weather','Cloud / weather'],['meteor','Meteor'],['satellite','Satellite'],['unknown','Unknown']];
  const presets=['ignore','bird/insect','aircraft','satellite','meteor','bird','insect','drone','star or planet','weather','lightning','artifact','unknown','interesting'];
  const human=value=>(value || 'unknown').replaceAll('_',' ');
  const labels=value=>value.split(',').map(v=>v.trim()).filter(Boolean);
  function selection(){
    el('event-selection-count').textContent=`${selected.size} selected${selected.size ? ' (across pages)' : ''}`;
    el('event-apply').disabled=el('event-export').disabled=!selected.size || mutating || !!pendingSaves.size;
    el('event-export-zip').disabled=!selected.size || mutating || exporting || !!pendingSaves.size;
    el('event-select-page').checked=rows.length>0 && rows.every(row=>selected.has(eventKey(row)));
    el('event-select-page').indeterminate=rows.some(row=>selected.has(eventKey(row))) && !el('event-select-page').checked;
  }
  function filters(){
    return {exported:el('event-exported').value || 'all',camera_id:el('event-camera').value || 'all',q:el('event-search').value.trim(),label:el('event-label-filter').value.trim(),reviewed:el('event-reviewed').value || 'all',protected:el('event-protected').value || 'all',reference:el('event-reference').value || 'all',state:el('event-state').value || 'all',type:el('event-type').value || 'all',trash:el('event-folder').value || 'active',sort:el('event-sort').value || 'newest',hide_likely_satellites:!!el('event-hide-likely').checked,start:el('event-start').value ? new Date(el('event-start').value).getTime()/1000:null,end:el('event-end').value ? new Date(el('event-end').value).getTime()/1000:null};
  }
  function setOptions(values){
    const list=el('event-label-options'), filter=el('event-label-filter'), previous=filter.value;
    const names=[...new Set([...presets,...values,...(previous?[previous]:[])])].sort();
    // A background refresh must leave an open dropdown intact when its options match.
    if(filter.options.length===names.length+1 && names.every((name,i)=>filter.options[i+1].value===name))return;
    list.replaceChildren();filter.replaceChildren();
    const any=node('option','Any label');any.value='';filter.append(any);
    for(const name of names){
      const option=node('option',name);option.value=name;list.append(option);
      const choice=node('option',name);choice.value=name;filter.append(choice);
    }
    filter.value=previous;
  }
  function node(tag,text,cls){const item=document.createElement(tag);if(text!=null)item.textContent=text;if(cls)item.className=cls;return item;}
  function button(text,action,cls='quiet'){const item=node('button',text,cls);item.type='button';item.onclick=action;return item;}
  const downloadFiles=[
    ['substream_preview','video','Small video (substream)','substream.mp4','review-substream-download'],
    ['original','original','Full-quality video (original)','original.mkv','review-original'],
    [null,'metadata','Event JSON','event.json','review-json-download'],
    ['best_frame','frame','Best-frame image','best-frame.jpg','review-frame-download'],
    ['thumbnail','thumbnail','Thumbnail','thumbnail.jpg','review-thumbnail-download']
  ];
  function downloadChoices(row){
    return downloadFiles.filter(([available])=>!available || row[available]).map(([,kind,label,suffix,id])=>({
      kind,label,id,href:eventPath(row,kind+'?download=1'),filename:`${row.camera_id || selectedCamera}-${row.id}-${suffix}`
    }));
  }
  function downloadMenu(row){
    const menu=node('details',null,'download-menu'),summary=node('summary','Download ▾','secondary');
    const links=node('div',null,'download-options');menu.append(summary,links);
    for(const choice of downloadChoices(row)){
      const link=node('a',choice.label);link.href=choice.href;link.download=choice.filename;
      link.onclick=()=>{menu.open=false;};links.append(link);
    }
    return menu;
  }
  function updateDownloads(row){
    const menu=el('review-download-menu');
    if(menu.dataset.eventKey!==eventKey(row))menu.open=false;
    menu.dataset.eventKey=eventKey(row);
    const choices=new Map(downloadChoices(row).map(choice=>[choice.id,choice]));
    for(const [,,,,id] of downloadFiles){
      const link=el(id),choice=choices.get(id);link.classList.toggle('hidden',!choice);
      link.removeAttribute('href');link.removeAttribute('download');
      if(choice){link.textContent=choice.label;link.href=choice.href;link.download=choice.filename;}
      link.onclick=()=>{menu.open=false;};
    }
  }
  function mount(page){
    if(page==='overview' || page==='events')el(page==='overview'?'recent-events':'event-archive').append(el('event-browser'));
  }
  function render(){
    const active=currentFilters || filters();
    const count=Object.entries(active).filter(([key,value])=>value!=null && value!=='' && value!==({exported:'all',camera_id:'all',reviewed:'all',protected:'all',reference:'all',state:'all',type:'all',trash:'active',sort:'newest',hide_likely_satellites:false}[key])).length;
    el('event-filter-summary').textContent=count ? `Filters · ${count} active · ${active.sort==='oldest'?'Oldest first':'Newest first'}`:'Filters · All cameras · Newest first';
    const body=el('all-events');body.replaceChildren();
    for(const sourceRow of rows){
      const row=withLocalReview(sourceRow);
      if(selected.has(eventKey(row)))selected.set(eventKey(row),row);
      const review=row.review || {}, tr=node('tr',null,'event-row');tr.dataset.eventId=row.id;tr.dataset.cameraId=row.camera_id;
      const choose=node('input');choose.type='checkbox';choose.checked=selected.has(eventKey(row));choose.setAttribute('aria-label',`Select event ${row.id}`);
      choose.onchange=()=>{if(choose.checked){if(selected.size>=100){el('event-action-status').textContent='Select up to 100 events at a time.';choose.checked=false;return;}selected.set(eventKey(row),row);}else selected.delete(eventKey(row));selection();};
      const cell=node('td');cell.append(choose);tr.append(cell);
      const image=node('td');const view=button('',()=>openReview(row),'event-row-image');view.setAttribute('aria-label',`Review event ${row.id}`);
      if(row.thumbnail){const img=node('img');img.src=eventPath(row,row.best_frame && !row.media_detail?.overlay_baked?'frame':'thumbnail');img.alt=row.best_frame?'Full-frame best image with estimated motion highlights':'Detection thumbnail';img.loading='lazy';view.append(img);
        const detail=row.media_detail;
        if(row.best_frame && !detail?.overlay_baked && detail?.target?.length===4 && detail.target.every(Number.isFinite)){
          const [x,y,w,h]=detail.target, box=node('span',null,'event-still-box');
          if(detail.native_width>0 && detail.native_height>0){view.style.aspectRatio=detail.native_width+'/'+detail.native_height;view.style.height='auto';img.style.height='auto';img.style.display='block';}
          const bw=Math.max(.04,w),bh=Math.max(.04,h);box.style.left=(Math.max(0,x-bw/2)*100)+'%';box.style.top=(Math.max(0,y-bh/2)*100)+'%';box.style.width=(Math.min(bw,1-Math.max(0,x-bw/2))*100)+'%';box.style.height=(Math.min(bh,1-Math.max(0,y-bh/2))*100)+'%';view.append(box);
        }}else view.textContent='Review';image.append(view);tr.append(image);
      tr.append(node('td',row.camera_name || row.camera_id || 'Camera 1'));
      const when=node('td');when.append(button(new Date(row.trigger_utc).toLocaleString(),()=>openReview(row),'event-time'),node('small',row.id));tr.append(when);
      tr.append(node('td',row.estimated_duration_seconds!=null ? `${Number(row.estimated_duration_seconds).toFixed(1)}s`:'—'));
      const annotation=node('td');const badges=node('div',null,'event-labels');for(const label of review.labels || [])badges.append(node('span',label,'event-label'));if(!review.labels?.length)badges.append(node('span','Unlabelled','muted'));
      annotation.append(badges,node('small',`${review.reviewed?'✓ Reviewed':'Unreviewed'}${review.protected?' · Protected':''}${review.trashed_at!=null?' · In trash':''}${review.annotation_error?' · Review metadata needs repair':''}`));if(row.export?.exported_unix)annotation.append(node('small','Exported '+new Date(row.export.exported_unix*1000).toLocaleString()+' · '+(row.export.media==='previews'?'previews':'originals + previews')));if(review.notes)annotation.append(node('small',review.notes.slice(0,100)));if(row.hailo_advisory?.advisory_text)annotation.append(node('small',row.hailo_advisory.advisory_text));tr.append(annotation);
      tr.append(node('td',human(row.identification)));const recording=node('td',human(row.state));recording.append(node('small',(row.detection_types || [row.trigger_type]).map(human).join(', ')));if(row.retention?.reason)recording.append(node('small',human(row.retention.reason)));tr.append(recording);
      const actions=node('td',null,'event-row-actions');actions.append(button('Review',()=>openReview(row),'secondary'));
      actions.append(downloadMenu(row));
      if(review.trashed_at!=null)actions.append(button('Restore',()=>mutate([row],'restore')));
      else {actions.append(button(review.protected?'Allow cleanup':'Keep',()=>mutate([row],'update',{protected:!review.protected})));const trash=button('Trash',()=>mutate([row],'trash'));trash.disabled=!!review.protected;actions.append(trash);}
      tr.append(actions);body.append(tr);
    }
    if(!rows.length){const tr=node('tr');const td=node('td','No events match these filters.');td.setAttribute('colspan','9');tr.append(td);body.append(tr);}
    el('event-page-status').textContent=total ? `${offset+1}–${offset+rows.length} of ${total} matching events`:'0 matching events';
    el('event-prev').disabled=offset===0;el('event-next').disabled=offset+rows.length>=total;
    el('event-trash-note').classList.toggle('hidden',currentFilters?.trash==='active');selection();
  }
  async function refresh(force=false){
    if(loading || !csrf || (!force && (el('review').open || (el('events').classList.contains('hidden') && el('overview').classList.contains('hidden')) || Date.now()-lastRefresh<8000)))return;
    loading=true;const version=requestVersion;
    try {if(!currentFilters)currentFilters=filters();const result=await api('/api/station/events/list',{...currentFilters,offset,limit:25});
      if(version!==requestVersion)return;rows=result.events.map(withLocalReview);total=result.total;offset=result.offset;
      if(offset>=total && offset>0){offset=Math.max(0,Math.floor(Math.max(0,total-1)/25)*25);lastRefresh=0;return;}
      setOptions(result.labels || []);if(reviewEvent){const updated=rows.find(row=>eventKey(row)===eventKey(reviewEvent));if(updated){const editing=dirty();reviewEvent=updated;updateReviewAvailability(updated);if(!editing)updateReview(updated,false);else reviewControls();}}loaded=true;lastRefresh=Date.now();render();el('event-badge').textContent=total;el('event-badge').title='Events matching current filters'+(result.unavailable_cameras?.length?' (some archives unavailable)':'');if(result.message)el('event-page-status').textContent+=' · '+result.message;
    }catch(error){el('event-page-status').textContent=error.message;}finally{loading=false;if(version!==requestVersion)setTimeout(()=>refresh(true),0);}
  }
  async function mutate(targetRows,action,changes){
    if(mutating || pendingSaves.size)return;mutating=true;selection();el('event-action-status').textContent='Saving event changes…';
    try {const result=await api('/api/station/events/update',{targets:targetRows.map(row=>({id:row.id,camera_id:row.camera_id || selectedCamera})),action,...(changes?{changes}:{})});
      for(const target of result.updated){selected.delete(eventKey(target));if(action==='update' && localReviews.has(eventKey(target)))localReviews.set(eventKey(target),{...localReviews.get(eventKey(target)),...labelReview(changes,withLocalReview(targetRows.find(row=>eventKey(row)===eventKey(target))))});}
      const failed=result.failed || [];el('event-action-status').textContent=`Updated ${result.updated.length} event${result.updated.length===1?'':'s'}.${action==='trash' && result.updated.length?' Restore them from the Trash folder if needed.':''}${failed.length?' '+failed.map(f=>`${f.id}: ${f.error}`).join(' '):''}`;
      lastRefresh=0;clipSignature='';await refresh(true);await windowRefresh();
    }catch(error){el('event-action-status').textContent=error.message;}finally{mutating=false;selection();}
  }
  async function windowRefresh(){await refreshDashboard();}
  function applyFilters(){if(typeof MagiFilterSelect!=='undefined')MagiFilterSelect.sync();requestVersion++;offset=0;selected.clear();currentFilters=filters();lastRefresh=0;rows=[];total=0;render();el('event-page-status').textContent='Loading filtered events…';return refresh(true);}
  function reviewControls(){
    const review=reviewEvent?.review || {}, busy=reviewBusy || mutating;
    el('review-event-prev').disabled=busy || !neighbors.previous;
    el('review-event-next').disabled=busy || !neighbors.next;
    el('review-mark').textContent=review.reviewed?'Mark unreviewed (R)':'Mark reviewed (R)';
    el('review-keep').textContent=review.protected?'Allow automatic cleanup':'Keep this event';
    el('review-trash').textContent=review.trashed_at!=null?'Restore from trash':'Move to trash (Delete)';
    const pendingCurrent=reviewEvent && pendingSaves.has(eventKey(reviewEvent));
    for(const id of ['review-mark','review-keep','review-save'])el(id).disabled=busy || pendingCurrent || review.trashed_at!=null || !!review.annotation_error;
    for(const [label] of quickLabels){const item=el('review-label-'+label.replace('/','-'));item.disabled=busy || advancingKey===eventKey(reviewEvent || {}) || (!pendingCurrent && pendingSaves.size>=100) || review.trashed_at!=null || !!review.annotation_error;item.setAttribute('aria-pressed',String((review.labels || []).includes(label)));}
    el('review-trash').disabled=busy || pendingCurrent || !!review.annotation_error || (review.trashed_at==null && !!review.protected);
  }
  function loadNeighbors(event){
    const version=++navigationVersion, query={...(currentFilters || filters())}, filterVersion=requestVersion;
    navigationKey=eventKey(event);navigationFilter=filterVersion;navigationReady=false;
    neighbors={previous:null,next:null};reviewControls();
    navigationTask=(async()=>{
      try {
        const result=await api('/api/station/events/list',{...query,anchor:{id:event.id,camera_id:event.camera_id || selectedCamera,trigger_unix:event.trigger_unix || 0}});
        if(version!==navigationVersion || filterVersion!==requestVersion || !reviewEvent || eventKey(event)!==eventKey(reviewEvent))return false;
        if(result.unavailable_cameras?.length)throw new Error('Some camera archives are unavailable.');
        neighbors={previous:result.previous,next:result.next};navigationReady=true;reviewControls();return true;
      }catch(error){if(version===navigationVersion)el('review-save-status').textContent='Navigation unavailable: '+error.message;return false;}
    })();
    return navigationTask;
  }
  function draftChanges(){return {labels:labels(el('review-labels').value),notes:el('review-notes').value,reviewed:el('review-reviewed').checked,protected:el('review-protected').checked};}
  function dirty(){
    const review=reviewEvent?.review || {}, draft=draftChanges();
    return JSON.stringify(draft.labels)!==JSON.stringify(review.labels || []) || draft.notes!==(review.notes || '') || draft.reviewed!==!!review.reviewed || draft.protected!==!!review.protected;
  }
  function mayLeave(){return !reviewBusy && (!dirty() || confirm('Discard unsaved review changes?'));}
  function navigate(direction){const target=direction<0?neighbors.previous:neighbors.next;if(target && mayLeave())openReview(target);}
  function updateReview(row, reloadNeighbors=true){
    if(!row)return;row=withLocalReview(row);if(reviewEvent && eventKey(reviewEvent)===eventKey(row))reviewEvent=row;const review=row.review || {};el('review-labels').value=(review.labels || []).join(', ');el('review-notes').value=review.notes || '';el('review-reviewed').checked=!!review.reviewed;el('review-protected').checked=!!review.protected;
    el('review-save-status').textContent=review.trashed_at!=null?'In trash. Restore before editing. Trash may be removed by retention or storage pressure.':review.protected?'Protected from automatic removal and trash.':'';
    reviewControls();if(reloadNeighbors)loadNeighbors(row);
  }
  async function saveReview(action='update',changes=draftChanges()){
    if(!reviewEvent || reviewBusy || mutating || pendingSaves.has(eventKey(reviewEvent)))return;
    const event=reviewEvent, key=eventKey(event);
    if(action==='update')changes=labelReview(changes,event);
    reviewBusy=true;reviewControls();el('review-save-status').textContent='Saving…';
    try {
      const result=await api('/api/station/events/update',{targets:[{id:event.id,camera_id:event.camera_id || selectedCamera}],action,...(action==='update'?{changes}:{})});
      if(!reviewEvent || eventKey(reviewEvent)!==key)return;
      if(result.failed.length){el('review-save-status').textContent=result.failed[0].error;return;}
      event.review={...event.review,...(action==='update'?changes:{trashed_at:action==='trash'?Date.now()/1000:null})};
      reviewEvent=event;
      if(action==='update' && localReviews.has(key))localReviews.set(key,{...localReviews.get(key),...changes});
      // Preserve unsaved labels/notes when using a quick action.
      if(action==='update' && changes.labels)el('review-labels').value=changes.labels.join(', ');
      if(action==='update' && 'reviewed' in changes)el('review-reviewed').checked=!!event.review.reviewed;
      if(action==='update' && 'protected' in changes)el('review-protected').checked=!!event.review.protected;
      el('review-save-status').textContent=action==='trash'?'Moved to trash. Use Restore from trash to undo; retention or storage pressure may remove trash.':action==='restore'?'Event restored.':'Review saved.';
      lastRefresh=0;clipSignature='';
      // Periodic refresh updates the list; reopening Events also refreshes it.
    }catch(error){if(reviewEvent && eventKey(reviewEvent)===key)el('review-save-status').textContent=error.message;}
    finally{reviewBusy=false;reviewControls();}
  }
  function init(){
    if(typeof window!=='undefined')window.addEventListener('beforeunload',event=>{if(pendingSaves.size){event.preventDefault();event.returnValue='';}});
    mount('overview');
    setOptions([]);
    for(const [label,title] of quickLabels){
      const item=el('review-label-'+label.replace('/','-'));
      item.onclick=()=>{
        if(item.disabled || !reviewEvent)return;
        const current=[...new Set(labels(el('review-labels').value).map(value=>value.toLowerCase()))];
        const next=current.includes(label)?current.filter(value=>value!==label):[...current,label];
        if(next.length>8){el('review-save-status').textContent='Up to eight labels per event. Remove a label before adding another.';return;}
        return queueReview({...draftChanges(),labels:next});
      };
      item.title=`Add or remove ${title.toLowerCase()}; saves in the background and opens the next matching event`;
    }
    el('event-filters').addEventListener('change',event=>{if(event.target.matches('select'))applyFilters();});
    el('event-filters').onsubmit=event=>{event.preventDefault();return applyFilters();};
    el('event-reset').onclick=()=>{for(const id of ['event-search','event-label-filter','event-start','event-end'])el(id).value='';for(const id of ['event-exported','event-reviewed','event-protected','event-state','event-reference','event-type'])el(id).value='all';el('event-hide-likely').checked=false;el('event-camera').value='all';el('event-folder').value='active';el('event-sort').value='newest';applyFilters();};
    el('event-select-page').onchange=()=>{for(const row of rows){if(el('event-select-page').checked && selected.size<100)selected.set(eventKey(row),row);else if(!el('event-select-page').checked)selected.delete(eventKey(row));}render();};
    el('event-clear-selection').onclick=()=>{selected.clear();render();};
    el('event-prev').onclick=()=>{offset=Math.max(0,offset-25);requestVersion++;refresh(true);};el('event-next').onclick=()=>{offset+=25;requestVersion++;refresh(true);};el('event-refresh').onclick=()=>refresh(true);
    el('event-apply').onclick=()=>{const action=el('event-action').value;const changes={reviewed:{reviewed:true},unreviewed:{reviewed:false},labels:{labels:labels(el('event-bulk-labels').value)},protect:{protected:true},unprotect:{protected:false}};return mutate([...selected.values()],action==='trash'||action==='restore'?action:'update',changes[action]);};
    el('event-export-zip').onclick=async()=>{
      if(!selected.size || mutating || exporting || pendingSaves.size)return;
      const targets=[...selected.values()].map(row=>({id:row.id,camera_id:row.camera_id || selectedCamera}));
      exporting=true;selection();
      try{
        const prepared=await api('/api/station/events/export',{targets,media:el('event-export-media').value});
        const link=node('a');link.href=prepared.download;link.download='';document.body.append(link);link.click();link.remove();
        el('event-action-status').textContent='Export download started. Watch browser downloads; keep this tab open for the export result.';
        for(let attempt=0;attempt<1800;attempt++){
          await new Promise(resolve=>setTimeout(resolve,2000));
          const status=await api('/api/station/events/export/'+prepared.id+'/status');
          if(status.state==='ready' || status.state==='sending')continue;
          if(status.state==='failed')throw new Error('Download interrupted. Events were not marked exported. Retry the selection.');
          el('event-action-status').textContent=
            `Sent ${status.sent} event(s). ${status.failed.length} unavailable; ${status.flag_failed.length} export flags could not be saved. Check manifest.json and your downloaded ZIP before relying on your copy.`;
          lastRefresh=0;await refresh(true);return;
        }
        throw new Error('Export status timed out. Check your browser download and refresh the event list.');
      }catch(error){el('event-action-status').textContent='Export: '+error.message;}
      finally{exporting=false;selection();}
    };
    el('event-export').onclick=async()=>{
      const chosen=[...selected.values()];
      if(!chosen.length || mutating)return;
      mutating=true;selection();
      try {
        const events=[];
        for(const row of chosen){
          const metadata=await api('/api/clips/'+encodeURIComponent(row.id)+'/metadata',undefined,row.camera_id || selectedCamera);
          let radio=null;
          if(metadata.radio_evidence_requested){try{radio=await api('/api/clips/'+encodeURIComponent(row.id)+'/radio',undefined,row.camera_id || selectedCamera);}catch{radio={status:'unavailable'};}}
          events.push({...row,...metadata,radio_evidence:radio,camera_id:row.camera_id,camera_name:row.camera_name,review:row.review});
        }
        const exported=new Date().toISOString();
        const suffix=Array.from(crypto.getRandomValues(new Uint8Array(8)),v=>v.toString(16).padStart(2,'0')).join('');
        const data={exported_utc:exported,events};
        const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));
        const a=node('a');a.href=url;
        const cameras=new Set(chosen.map(row=>row.camera_id || selectedCamera));
        const safe=value=>String(value || '').replace(/[^a-zA-Z0-9_-]+/g,'-').replace(/^-+|-+$/g,'').slice(0,60);
        const camera=cameras.size===1 ? [safe(chosen[0].camera_name),safe(chosen[0].camera_id || selectedCamera)].filter(Boolean).join('-') : 'multiple-cameras';
        a.download='magi-events-'+camera+'-'+exported.replace(/[:.]/g,'-')+'-'+suffix+'.json';
        a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
        el('event-action-status').textContent=`Exported ${events.length} event(s) with full metadata.`;
      }catch(error){el('event-action-status').textContent='Export failed: '+error.message;}
      finally{mutating=false;selection();}
    };
    el('review-save').onclick=()=>saveReview();
    el('review-mark').onclick=()=>saveReview('update',{reviewed:!reviewEvent.review?.reviewed});
    el('review-keep').onclick=()=>saveReview('update',{protected:!reviewEvent.review?.protected});
    el('review-trash').onclick=()=>{if(!el('review-trash').disabled && mayLeave())return saveReview(reviewEvent.review?.trashed_at!=null?'restore':'trash');};
    el('review-event-prev').onclick=()=>navigate(-1);el('review-event-next').onclick=()=>navigate(1);
    el('review').addEventListener('keydown',event=>{
      if(!el('review').open || event.defaultPrevented || event.repeat || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey || reviewBusy || mutating)return;
      const tag=event.target?.tagName?.toLowerCase();
      if(['input','textarea','select','video'].includes(tag) || event.target?.isContentEditable)return;
      const key=event.key.toLowerCase();
      if(key==='arrowleft' || key==='arrowright'){event.preventDefault();navigate(key==='arrowleft'?-1:1);}
      else if(key==='r'){event.preventDefault();if(!el('review-mark').disabled)el('review-mark').onclick();}
      else if(key==='delete'){event.preventDefault();if(reviewEvent?.review?.trashed_at==null)el('review-trash').onclick();}
      else if(key===' ' && tag!=='button'){
        event.preventDefault();if(el('review-play').disabled)return;
        const video=el('review-video');if(reviewMode!=='video')el('review-play').onclick();else if(video.paused)video.play().catch(()=>{});else video.pause();
      }
    });
    el('review').addEventListener('close',()=>{refresh(true);});
    el('review').addEventListener('cancel',event=>{if(!mayLeave())event.preventDefault();});
  }

  function reset(){navigationVersion++;navigationTask=null;navigationKey=null;navigationReady=false;neighbors={previous:null,next:null};selected.clear();rows=[];loaded=false;currentFilters=null;lastRefresh=0;requestVersion++;selection();}
  return {init,mount,refresh,updateReview,reset,mayLeave,withLocalReview,downloadChoices,updateDownloads,hasPending:()=>!!pendingSaves.size,loaded:()=>loaded};
})();
