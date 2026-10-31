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
    el('event-select-page').checked=rows.length>0 && rows.every(row=>selected.has(row.id));
    el('event-select-page').indeterminate=rows.some(row=>selected.has(row.id)) && !el('event-select-page').checked;
  }
  function filters(){
    return {q:el('event-search').value.trim(),label:el('event-label-filter').value.trim(),reviewed:el('event-reviewed').value || 'all',protected:el('event-protected').value || 'all',reference:el('event-reference').value || 'all',state:el('event-state').value || 'all',type:el('event-type').value || 'all',trash:el('event-folder').value || 'active',sort:el('event-sort').value || 'newest',start:el('event-start').value ? new Date(el('event-start').value).getTime()/1000:null,end:el('event-end').value ? new Date(el('event-end').value).getTime()/1000:null};
  }
  function setOptions(values){
    const list=el('event-label-options');list.replaceChildren();
    for(const name of [...new Set([...presets,...values])].sort()){const option=document.createElement('option');option.value=name;list.append(option);}
  }
  function node(tag,text,cls){const item=document.createElement(tag);if(text!=null)item.textContent=text;if(cls)item.className=cls;return item;}
  function button(text,action,cls='quiet'){const item=node('button',text,cls);item.type='button';item.onclick=action;return item;}
  function render(){
    const body=el('all-events');body.replaceChildren();
    for(const row of rows){
      if(selected.has(row.id))selected.set(row.id,row);
      const review=row.review || {}, tr=node('tr',null,'event-row');tr.dataset.eventId=row.id;
      const choose=node('input');choose.type='checkbox';choose.checked=selected.has(row.id);choose.setAttribute('aria-label',`Select event ${row.id}`);
      choose.onchange=()=>{if(choose.checked){if(selected.size>=100){el('event-action-status').textContent='Select up to 100 events at a time.';choose.checked=false;return;}selected.set(row.id,row);}else selected.delete(row.id);selection();};
      const cell=node('td');cell.append(choose);tr.append(cell);
      const image=node('td');const view=button('',()=>openReview(row),'event-row-image');view.setAttribute('aria-label',`Review event ${row.id}`);
      if(row.thumbnail){const img=node('img');img.src=cameraPath(`/api/clips/${encodeURIComponent(row.id)}/thumbnail`);img.alt=row.best_frame?'Selected full-resolution event crop':'Detection thumbnail';img.loading='lazy';view.append(img);}else view.textContent='Review';image.append(view);tr.append(image);
      const when=node('td');when.append(button(new Date(row.trigger_utc).toLocaleString(),()=>openReview(row),'event-time'),node('small',row.id));tr.append(when);
      tr.append(node('td',row.estimated_duration_seconds!=null ? `${Number(row.estimated_duration_seconds).toFixed(1)}s`:'—'));
      const annotation=node('td');const badges=node('div',null,'event-labels');for(const label of review.labels || [])badges.append(node('span',label,'event-label'));if(!review.labels?.length)badges.append(node('span','Unlabelled','muted'));
      annotation.append(badges,node('small',`${review.reviewed?'✓ Reviewed':'Unreviewed'}${review.protected?' · Protected':''}${review.trashed_at!=null?' · In trash':''}${review.annotation_error?' · Review metadata needs repair':''}`));if(review.notes)annotation.append(node('small',review.notes.slice(0,100)));tr.append(annotation);
      tr.append(node('td',human(row.identification)));const recording=node('td',human(row.state));recording.append(node('small',(row.detection_types || [row.trigger_type]).map(human).join(', ')));if(row.retention?.reason)recording.append(node('small',human(row.retention.reason)));tr.append(recording);
      const actions=node('td',null,'event-row-actions');actions.append(button('Review',()=>openReview(row),'secondary'));
      if(row.original){const a=node('a','Original ↓');a.href=cameraPath(`/api/clips/${encodeURIComponent(row.id)}/original`);actions.append(a);}
      if(review.trashed_at!=null)actions.append(button('Restore',()=>mutate([row.id],'restore')));
      else {actions.append(button(review.protected?'Unprotect':'Protect',()=>mutate([row.id],'update',{protected:!review.protected})));const trash=button('Trash',()=>mutate([row.id],'trash'));trash.disabled=!!review.protected;actions.append(trash);}
      tr.append(actions);body.append(tr);
    }
    if(!rows.length){const tr=node('tr');const td=node('td','No events match these filters.');td.setAttribute('colspan','8');tr.append(td);body.append(tr);}
    el('event-page-status').textContent=total ? `${offset+1}–${offset+rows.length} of ${total} matching events`:'0 matching events';
    el('event-prev').disabled=offset===0;el('event-next').disabled=offset+rows.length>=total;
    el('event-trash-note').classList.toggle('hidden',currentFilters?.trash==='active');selection();
  }
  async function refresh(force=false){
    if(loading || !csrf || (!force && (el('events').classList.contains('hidden') || Date.now()-lastRefresh<8000)))return;
    loading=true;const version=requestVersion;
    try {if(!currentFilters)currentFilters=filters();const result=await api('/api/events/list',{...currentFilters,offset,limit:25});
      if(version!==requestVersion)return;rows=result.events;total=result.total;offset=result.offset;
      if(offset>=total && offset>0){offset=Math.max(0,Math.floor(Math.max(0,total-1)/25)*25);lastRefresh=0;return;}
      setOptions(result.labels || []);loaded=true;lastRefresh=Date.now();render();if(result.message)el('event-page-status').textContent=result.message;
    }catch(error){el('event-page-status').textContent=error.message;}finally{loading=false;if(version!==requestVersion)setTimeout(()=>refresh(true),0);}
  }
  async function mutate(ids,action,changes){
    if(mutating)return;mutating=true;selection();el('event-action-status').textContent='Saving event changes…';
    try {const result=await api('/api/events/update',{ids,action,...(changes?{changes}:{})});
      for(const id of result.updated)selected.delete(id);
      const failed=result.failed || [];el('event-action-status').textContent=`Updated ${result.updated.length} event${result.updated.length===1?'':'s'}.${action==='trash' && result.updated.length?' Restore them from the Trash folder if needed.':''}${failed.length?' '+failed.map(f=>`${f.id}: ${f.error}`).join(' '):''}`;
      lastRefresh=0;clipSignature='';await refresh(true);await windowRefresh();
    }catch(error){el('event-action-status').textContent=error.message;}finally{mutating=false;selection();}
  }
  async function windowRefresh(){await refreshDashboard();}
  function applyFilters(){requestVersion++;offset=0;selected.clear();currentFilters=filters();lastRefresh=0;rows=[];total=0;render();el('event-page-status').textContent='Loading filtered events…';return refresh(true);}
  function navigate(id,direction){const index=rows.findIndex(row=>row.id===id), target=rows[index+direction];if(index>=0 && target)openReview(target);}
  function updateReview(row){
    if(!row)return;const review=row.review || {};el('review-labels').value=(review.labels || []).join(', ');el('review-notes').value=review.notes || '';el('review-reviewed').checked=!!review.reviewed;el('review-protected').checked=!!review.protected;
    el('review-save-status').textContent=review.trashed_at!=null?'Restore this event from Trash before editing its review.':'';el('review-save').disabled=review.trashed_at!=null || !!review.annotation_error;
    const index=rows.findIndex(r=>r.id===row.id);el('review-event-prev').disabled=index<=0;el('review-event-next').disabled=index<0 || index>=rows.length-1;
  }
  function init(){
    setOptions([]);
    el('event-filters').onsubmit=event=>{event.preventDefault();return applyFilters();};
    el('event-reset').onclick=()=>{for(const id of ['event-search','event-label-filter','event-start','event-end'])el(id).value='';for(const id of ['event-reviewed','event-protected','event-state','event-reference','event-type'])el(id).value='all';el('event-folder').value='active';el('event-sort').value='newest';applyFilters();};
    el('event-select-page').onchange=()=>{for(const row of rows){if(el('event-select-page').checked && selected.size<100)selected.set(row.id,row);else if(!el('event-select-page').checked)selected.delete(row.id);}render();};
    el('event-clear-selection').onclick=()=>{selected.clear();render();};
    el('event-prev').onclick=()=>{offset=Math.max(0,offset-25);requestVersion++;refresh(true);};el('event-next').onclick=()=>{offset+=25;requestVersion++;refresh(true);};el('event-refresh').onclick=()=>refresh(true);
    el('event-apply').onclick=()=>{const action=el('event-action').value;const changes={reviewed:{reviewed:true},unreviewed:{reviewed:false},labels:{labels:labels(el('event-bulk-labels').value)},protect:{protected:true},unprotect:{protected:false}};return mutate([...selected.keys()],action==='trash'||action==='restore'?action:'update',changes[action]);};
    el('event-export').onclick=()=>{const data={exported_utc:new Date().toISOString(),events:[...selected.values()]};const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const a=node('a');a.href=url;a.download='magi-selected-events.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
    el('review-save').onclick=async()=>{if(!reviewEvent)return;el('review-save').disabled=true;const id=reviewEvent.id;try {const result=await api('/api/events/update',{ids:[id],action:'update',changes:{labels:labels(el('review-labels').value),notes:el('review-notes').value,reviewed:el('review-reviewed').checked,protected:el('review-protected').checked}});el('review-save-status').textContent=result.failed.length?result.failed[0].error:'Review saved.';if(result.updated.length){lastRefresh=0;clipSignature='';await refresh(true);await refreshDashboard();}}catch(error){el('review-save-status').textContent=error.message;}finally{el('review-save').disabled=false;}};
    el('review-event-prev').onclick=()=>navigate(reviewEvent?.id,-1);el('review-event-next').onclick=()=>navigate(reviewEvent?.id,1);
  }
  function reset(){selected.clear();rows=[];loaded=false;currentFilters=null;lastRefresh=0;requestVersion++;selection();}
  return {init,refresh,updateReview,reset,loaded:()=>loaded};
})();
