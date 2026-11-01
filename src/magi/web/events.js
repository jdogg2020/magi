'use strict';
const MagiEvents = (()=>{
  const el=id=>document.getElementById(id), selected=new Map();
  let rows=[], total=0, offset=0, loaded=false, loading=false, lastRefresh=0, requestVersion=0, mutating=false, currentFilters=null;
  const presets=['aircraft','satellite','meteor','bird','insect','drone','star or planet','weather','lightning','artifact','unknown','interesting'];
  const human=value=>(value || 'unknown').replaceAll('_',' ');
  const labels=value=>value.split(',').map(v=>v.trim()).filter(Boolean);
  function selection(){
    el('event-selection-count').textContent=`${selected.size} selected${selected.size ? ' (across pages)' : ''}`;
    el('event-apply').disabled=el('event-export').disabled=!selected.size || mutating;
    el('event-select-page').checked=rows.length>0 && rows.every(row=>selected.has(eventKey(row)));
    el('event-select-page').indeterminate=rows.some(row=>selected.has(eventKey(row))) && !el('event-select-page').checked;
  }
  function filters(){
    return {camera_id:el('event-camera').value || 'all',q:el('event-search').value.trim(),label:el('event-label-filter').value.trim(),reviewed:el('event-reviewed').value || 'all',protected:el('event-protected').value || 'all',reference:el('event-reference').value || 'all',state:el('event-state').value || 'all',type:el('event-type').value || 'all',trash:el('event-folder').value || 'active',sort:el('event-sort').value || 'newest',start:el('event-start').value ? new Date(el('event-start').value).getTime()/1000:null,end:el('event-end').value ? new Date(el('event-end').value).getTime()/1000:null};
  }
  function setOptions(values){
    const list=el('event-label-options');list.replaceChildren();
    for(const name of [...new Set([...presets,...values])].sort()){const option=document.createElement('option');option.value=name;list.append(option);}
  }
  function node(tag,text,cls){const item=document.createElement(tag);if(text!=null)item.textContent=text;if(cls)item.className=cls;return item;}
  function button(text,action,cls='quiet'){const item=node('button',text,cls);item.type='button';item.onclick=action;return item;}
  function mount(page){
    if(page==='overview' || page==='events')el(page==='overview'?'recent-events':'event-archive').append(el('event-browser'));
  }
  function render(){
    const active=currentFilters || filters();
    const count=Object.entries(active).filter(([key,value])=>value!=null && value!=='' && value!==({camera_id:'all',reviewed:'all',protected:'all',reference:'all',state:'all',type:'all',trash:'active',sort:'newest'}[key])).length;
    el('event-filter-summary').textContent=count ? `Filters · ${count} active · ${active.sort==='oldest'?'Oldest first':'Newest first'}`:'Filters · All cameras · Newest first';
    const body=el('all-events');body.replaceChildren();
    for(const row of rows){
      if(selected.has(eventKey(row)))selected.set(eventKey(row),row);
      const review=row.review || {}, tr=node('tr',null,'event-row');tr.dataset.eventId=row.id;tr.dataset.cameraId=row.camera_id;
      const choose=node('input');choose.type='checkbox';choose.checked=selected.has(eventKey(row));choose.setAttribute('aria-label',`Select event ${row.id}`);
      choose.onchange=()=>{if(choose.checked){if(selected.size>=100){el('event-action-status').textContent='Select up to 100 events at a time.';choose.checked=false;return;}selected.set(eventKey(row),row);}else selected.delete(eventKey(row));selection();};
      const cell=node('td');cell.append(choose);tr.append(cell);
      const image=node('td');const view=button('',()=>openReview(row),'event-row-image');view.setAttribute('aria-label',`Review event ${row.id}`);
      if(row.thumbnail){const img=node('img');img.src=eventPath(row,'thumbnail');img.alt=row.best_frame?'Selected full-resolution event crop':'Detection thumbnail';img.loading='lazy';view.append(img);}else view.textContent='Review';image.append(view);tr.append(image);
      tr.append(node('td',row.camera_name || row.camera_id || 'Camera 1'));
      const when=node('td');when.append(button(new Date(row.trigger_utc).toLocaleString(),()=>openReview(row),'event-time'),node('small',row.id));tr.append(when);
      tr.append(node('td',row.estimated_duration_seconds!=null ? `${Number(row.estimated_duration_seconds).toFixed(1)}s`:'—'));
      const annotation=node('td');const badges=node('div',null,'event-labels');for(const label of review.labels || [])badges.append(node('span',label,'event-label'));if(!review.labels?.length)badges.append(node('span','Unlabelled','muted'));
      annotation.append(badges,node('small',`${review.reviewed?'✓ Reviewed':'Unreviewed'}${review.protected?' · Protected':''}${review.trashed_at!=null?' · In trash':''}${review.annotation_error?' · Review metadata needs repair':''}`));if(review.notes)annotation.append(node('small',review.notes.slice(0,100)));tr.append(annotation);
      tr.append(node('td',human(row.identification)));const recording=node('td',human(row.state));recording.append(node('small',(row.detection_types || [row.trigger_type]).map(human).join(', ')));if(row.retention?.reason)recording.append(node('small',human(row.retention.reason)));tr.append(recording);
      const actions=node('td',null,'event-row-actions');actions.append(button('Review',()=>openReview(row),'secondary'));
      if(row.original){const a=node('a','Original ↓');a.href=eventPath(row,'original');actions.append(a);}
      if(review.trashed_at!=null)actions.append(button('Restore',()=>mutate([row],'restore')));
      else {actions.append(button(review.protected?'Unprotect':'Protect',()=>mutate([row],'update',{protected:!review.protected})));const trash=button('Trash',()=>mutate([row],'trash'));trash.disabled=!!review.protected;actions.append(trash);}
      tr.append(actions);body.append(tr);
    }
    if(!rows.length){const tr=node('tr');const td=node('td','No events match these filters.');td.setAttribute('colspan','9');tr.append(td);body.append(tr);}
    el('event-page-status').textContent=total ? `${offset+1}–${offset+rows.length} of ${total} matching events`:'0 matching events';
    el('event-prev').disabled=offset===0;el('event-next').disabled=offset+rows.length>=total;
    el('event-trash-note').classList.toggle('hidden',currentFilters?.trash==='active');selection();
  }
  async function refresh(force=false){
    if(loading || !csrf || (!force && ((el('events').classList.contains('hidden') && el('overview').classList.contains('hidden')) || Date.now()-lastRefresh<8000)))return;
    loading=true;const version=requestVersion;
    try {if(!currentFilters)currentFilters=filters();const result=await api('/api/station/events/list',{...currentFilters,offset,limit:25});
      if(version!==requestVersion)return;rows=result.events;total=result.total;offset=result.offset;
      if(offset>=total && offset>0){offset=Math.max(0,Math.floor(Math.max(0,total-1)/25)*25);lastRefresh=0;return;}
      setOptions(result.labels || []);if(reviewEvent){const updated=rows.find(row=>eventKey(row)===eventKey(reviewEvent));if(updated){reviewEvent=updated;updateReviewAvailability(updated);}}loaded=true;lastRefresh=Date.now();render();if(result.message)el('event-page-status').textContent+=' · '+result.message;
    }catch(error){el('event-page-status').textContent=error.message;}finally{loading=false;if(version!==requestVersion)setTimeout(()=>refresh(true),0);}
  }
  async function mutate(targetRows,action,changes){
    if(mutating)return;mutating=true;selection();el('event-action-status').textContent='Saving event changes…';
    try {const result=await api('/api/station/events/update',{targets:targetRows.map(row=>({id:row.id,camera_id:row.camera_id || selectedCamera})),action,...(changes?{changes}:{})});
      for(const target of result.updated)selected.delete(eventKey(target));
      const failed=result.failed || [];el('event-action-status').textContent=`Updated ${result.updated.length} event${result.updated.length===1?'':'s'}.${action==='trash' && result.updated.length?' Restore them from the Trash folder if needed.':''}${failed.length?' '+failed.map(f=>`${f.id}: ${f.error}`).join(' '):''}`;
      lastRefresh=0;clipSignature='';await refresh(true);await windowRefresh();
    }catch(error){el('event-action-status').textContent=error.message;}finally{mutating=false;selection();}
  }
  async function windowRefresh(){await refreshDashboard();}
  function applyFilters(){requestVersion++;offset=0;selected.clear();currentFilters=filters();lastRefresh=0;rows=[];total=0;render();el('event-page-status').textContent='Loading filtered events…';return refresh(true);}
  function navigate(event,direction){const index=rows.findIndex(row=>event && eventKey(row)===eventKey(event)), target=rows[index+direction];if(index>=0 && target)openReview(target);}
  function updateReview(row){
    if(!row)return;const review=row.review || {};el('review-labels').value=(review.labels || []).join(', ');el('review-notes').value=review.notes || '';el('review-reviewed').checked=!!review.reviewed;el('review-protected').checked=!!review.protected;
    el('review-save-status').textContent=review.trashed_at!=null?'Restore this event from Trash before editing its review.':'';el('review-save').disabled=review.trashed_at!=null || !!review.annotation_error;
    const index=rows.findIndex(r=>eventKey(r)===eventKey(row));el('review-event-prev').disabled=index<=0;el('review-event-next').disabled=index<0 || index>=rows.length-1;
  }
  function init(){
    mount('overview');
    setOptions([]);
    el('event-filters').onsubmit=event=>{event.preventDefault();return applyFilters();};
    el('event-reset').onclick=()=>{for(const id of ['event-search','event-label-filter','event-start','event-end'])el(id).value='';for(const id of ['event-reviewed','event-protected','event-state','event-reference','event-type'])el(id).value='all';el('event-camera').value='all';el('event-folder').value='active';el('event-sort').value='newest';applyFilters();};
    el('event-select-page').onchange=()=>{for(const row of rows){if(el('event-select-page').checked && selected.size<100)selected.set(eventKey(row),row);else if(!el('event-select-page').checked)selected.delete(eventKey(row));}render();};
    el('event-clear-selection').onclick=()=>{selected.clear();render();};
    el('event-prev').onclick=()=>{offset=Math.max(0,offset-25);requestVersion++;refresh(true);};el('event-next').onclick=()=>{offset+=25;requestVersion++;refresh(true);};el('event-refresh').onclick=()=>refresh(true);
    el('event-apply').onclick=()=>{const action=el('event-action').value;const changes={reviewed:{reviewed:true},unreviewed:{reviewed:false},labels:{labels:labels(el('event-bulk-labels').value)},protect:{protected:true},unprotect:{protected:false}};return mutate([...selected.values()],action==='trash'||action==='restore'?action:'update',changes[action]);};
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
    el('review-save').onclick=async()=>{if(!reviewEvent)return;el('review-save').disabled=true;const event=reviewEvent;try {const result=await api('/api/station/events/update',{targets:[{id:event.id,camera_id:event.camera_id || selectedCamera}],action:'update',changes:{labels:labels(el('review-labels').value),notes:el('review-notes').value,reviewed:el('review-reviewed').checked,protected:el('review-protected').checked}});el('review-save-status').textContent=result.failed.length?result.failed[0].error:'Review saved.';if(result.updated.length){lastRefresh=0;clipSignature='';await refresh(true);await refreshDashboard();}}catch(error){el('review-save-status').textContent=error.message;}finally{el('review-save').disabled=false;}};
    el('review-event-prev').onclick=()=>navigate(reviewEvent,-1);el('review-event-next').onclick=()=>navigate(reviewEvent,1);
  }
  function reset(){selected.clear();rows=[];loaded=false;currentFilters=null;lastRefresh=0;requestVersion++;selection();}
  return {init,mount,refresh,updateReview,reset,loaded:()=>loaded};
})();
