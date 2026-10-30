/* Independent review geometry for Magi's recorded detections. */
const MagiTracks = (() => {
  function clipSeconds(segments, host) {
    if (!Number.isFinite(host) || !Array.isArray(segments)) return null;
    let offset = 0;
    for (const s of segments) {
      const values = [s.media_start,s.media_end,s.host_start_unix,s.host_end_unix];
      if (!values.every(Number.isFinite) || s.media_end <= s.media_start || s.host_end_unix <= s.host_start_unix) return null;
      const duration = s.media_end-s.media_start;
      if (host >= s.host_start_unix && host <= s.host_end_unix) return offset + duration*(host-s.host_start_unix)/(s.host_end_unix-s.host_start_unix);
      offset += duration;
    }
    return null;
  }
  function collect(samples, meta, segments = null) {
    if (!meta || !(meta.width > 0 && meta.height > 0)) return [];
    const groups = new Map();
    for (const row of samples) {
      const t = segments ? clipSeconds(segments,row.host_unix) : row.video_seconds;
      if (!Number.isFinite(t) || t < 0 || row.suppressed) continue;
      for (const obj of (row.objects || []).slice(0,32)) {
        const x=obj.x/meta.width, y=obj.y/meta.height;
        if (!obj.track_id || !Number.isFinite(x) || !Number.isFinite(y) || x<0 || x>1 || y<0 || y>1) continue;
        const list=groups.get(obj.track_id)||[];
        if (!list.length || t>list[list.length-1][0]) list.push([t,x,y]);
        groups.set(obj.track_id,list);
      }
    }
    const tracks=[];
    for (const [id, points] of groups) {
      let part=[];
      for (const p of points) {
        if (part.length && p[0]-part[part.length-1][0]>.75) { if(part.length>=2) tracks.push({id,points:part}); part=[]; }
        part.push(p);
      }
      if(part.length>=2) tracks.push({id,points:part});
    }
    return tracks.sort((a,b)=>b.points.length-a.points.length).slice(0,8);
  }
  function position(points, seconds) {
    if (!points.length || !Number.isFinite(seconds)) return null;
    const first=points[0], last=points[points.length-1];
    if(seconds<first[0]) return first[0]-seconds<=.15 ? first : null;
    if(seconds>last[0]) return seconds-last[0]<=.15 ? last : null;
    for(let i=1;i<points.length;i++) if(seconds<=points[i][0]) {
      const a=points[i-1], b=points[i], f=(seconds-a[0])/(b[0]-a[0]);
      return [seconds,a[1]+(b[1]-a[1])*f,a[2]+(b[2]-a[2])*f];
    }
    return last;
  }
  return {clipSeconds,collect,position};
})();
if(typeof module !== 'undefined' && module.exports) module.exports=MagiTracks;
