"""Bounded shared local ADS-B history and immutable event evidence. No network access."""
from collections import deque
import json
import math
import re
import threading
import time
from .sensors import AIRCRAFT, bounded_json, number

FIELDS = set('hex flight type lat lon alt_geom alt_baro gs track track_rate baro_rate geom_rate ias tas mach roll mag_heading true_heading squawk emergency category nic rc nac_p nac_v sil sil_type gva sda nic_baro version seen seen_pos messages rssi alert spi nav_qnh mlat tisb'.split())
MAX_EVENT = 512_000


def snapshot(data, received):
    if not isinstance(data,dict) or not number(data.get('now')) or not isinstance(data.get('aircraft'),list):
        raise ValueError('Invalid receiver snapshot')
    rows=[]
    for row in data['aircraft'][:512]:
        if not isinstance(row,dict) or not isinstance(row.get('hex'),str) or not re.fullmatch(r'~?[0-9a-fA-F]{6}',row['hex']):
            continue
        saved={}
        for key in FIELDS & row.keys():
            value=row[key]
            if number(value):
                saved[key]=value
            elif isinstance(value,str) and len(value)<=80 and value.isprintable():
                saved[key]=value.strip()
            elif key in ('mlat','tisb') and isinstance(value,list):
                saved[key]=[v for v in value[:32] if isinstance(v,str) and len(v)<=40]
        rows.append(saved)
    return {'received_unix':received,'feed_unix':data['now'],
            'status':'fresh' if abs(received-data['now'])<=10 else 'stale',
            'aircraft':rows,'rows_truncated':len(data['aircraft'])>512}


class Radio:
    def __init__(self, path=AIRCRAFT, *, start=True):
        self.path=path
        self.lock=threading.Lock()
        self.frames=deque()
        self.size=0
        self.stop=threading.Event()
        self.thread=threading.Thread(target=self.run,name='magi-radio-history',daemon=True)
        if start:self.thread.start()

    def poll(self):
        now=time.time()
        try:
            frame=snapshot(bounded_json(self.path),now)
        except (OSError,ValueError,TypeError):
            frame={'received_unix':now,'status':'unavailable','aircraft':[]}
        payload=json.dumps(frame,separators=(',',':'),allow_nan=False)
        with self.lock:
            # Backward clock steps cannot mix incompatible time histories.
            if self.frames and now<self.frames[-1][0]:
                self.frames.clear();self.size=0
            self.frames.append((now,payload));self.size+=len(payload)
            while self.frames and (self.size>8_000_000 or len(self.frames)>450 or now-self.frames[0][0]>900):
                self.size-=len(self.frames.popleft()[1])

    def run(self):
        while not self.stop.is_set():
            self.poll();self.stop.wait(2)

    def window(self, meta):
        trigger=meta.get('trigger_unix')
        if not number(trigger):raise ValueError('Invalid event time')
        pre=meta.get('requested_pre_roll_seconds',0)
        end=meta.get('requested_end_unix',trigger)
        if not number(pre) or not number(end):raise ValueError('Invalid event window')
        start=trigger-max(0,min(pre,120))-5
        end=max(trigger,min(end,trigger+600))
        with self.lock:
            selected=[payload for stamp,payload in self.frames if start<=stamp<=end]
        kept=[];size=0
        for payload in reversed(selected):
            frame=json.loads(payload)
            expanded=json.dumps(frame,indent=2,allow_nan=False)
            cost=len(expanded)+6*(expanded.count('\n')+1)+2
            if size+cost>MAX_EVENT-4096:break
            kept.append(frame);size+=cost
        kept.reverse()
        fresh=[f for f in kept if f['status']=='fresh']
        stamps=[f['received_unix'] for f in kept]
        truncated=len(kept)<len(selected) or any(f.get('rows_truncated') for f in kept)
        covered=bool(stamps) and stamps[0]<=start+3 and stamps[-1]>=end-3 and all(b-a<=5 for a,b in zip(stamps,stamps[1:]))
        return {'schema':1,'event_id':meta['id'],'source':'local_readsb','saved_unix':time.time(),
                'requested_start_unix':start,'requested_end_unix':end,
                'coverage':'complete' if covered and len(fresh)==len(kept) and not truncated else 'partial' if fresh else 'unavailable',
                'truncated':truncated,'snapshots':kept,
                'aircraft_count':len({r['hex'] for f in fresh for r in f['aircraft']}),
                'units':{'lat_lon':'degrees','alt_geom':'feet WGS84','alt_baro':'feet pressure altitude or ground',
                         'gs':'knots','track':'degrees true','baro_rate_geom_rate':'feet/minute','rssi':'dBFS'},
                'limitations':['Receiver snapshots, not raw messages; individual field ages may be unknown.',
                                'Pi host timing, not verified camera exposure time. Missing coverage does not exclude aircraft.',
                                'Buffer/startup gaps and truncation are explicit; nearby radio traffic alone is not a visual association.']}

    def close(self):
        self.stop.set()
        if self.thread.is_alive():self.thread.join(3)


def match(evidence, samples, rays, settings):
    """Temporal position/motion association; bearing-only is explicitly weaker."""
    from .identification import aircraft_direction, angular, unit, score_track
    groups={}
    for frame in evidence.get('snapshots',[]):
        if frame.get('status')!='fresh':continue
        for row in frame.get('aircraft',[]):
            if row.get('alt_baro')=='ground':continue
            if not all(number(row.get(k)) for k in ('lat','lon','seen_pos')):continue
            if not (-90<=row['lat']<=90 and -180<=row['lon']<=180 and 0<=row['seen_pos']<=5):continue
            stamp=frame['feed_unix']-row['seen_pos']
            position={'id':row['hex'],'name':row.get('flight') or row['hex'],'time':stamp,
                      'latitude':row['lat'],'longitude':row['lon'],'elevation_m':row['alt_geom']*.3048 if number(row.get('alt_geom')) else None}
            groups.setdefault(row['hex'],{})[stamp]=position
    candidates=[];evaluated=0
    for identity, by_time in groups.items():
        positions=sorted(by_time.values(),key=lambda p:p['time'])
        chosen=[min(positions,key=lambda p:abs(p['time']-s['time'])) for s in samples]
        if any(abs(p['time']-s['time'])>2 for p,s in zip(chosen,samples)) or len({p['time'] for p in chosen})<2:continue
        bearing_only=any(p['elevation_m'] is None for p in chosen)
        if bearing_only:
            if any(math.hypot(r[0],r[1])<.1 for r in rays):continue
            observed=[unit((r[0],r[1],0)) for r in rays]
            predicted=[]
            for p in chosen:
                lat1,lat2=map(math.radians,(settings['latitude'],p['latitude']))
                delta=math.radians(p['longitude']-settings['longitude'])
                east=math.sin(delta)*math.cos(lat2)
                north=math.cos(lat1)*math.sin(lat2)-math.sin(lat1)*math.cos(lat2)*math.cos(delta)
                if math.hypot(east,north)<1e-8:break
                predicted.append(unit((east,north,0)))
            if len(predicted)!=len(samples):continue
        else:
            observed=rays;predicted=[aircraft_direction(p,settings) for p in chosen]
        evaluated+=1
        score=score_track(observed,predicted,settings['tolerance_deg'])
        if not score:continue
        duration=samples[-1]['time']-samples[0]['time']
        if duration<=0:continue
        actual=angular(observed[0],observed[-1])/duration
        expected=angular(predicted[0],predicted[-1])/duration
        if expected<=0 or not .25<=actual/expected<=4:continue
        candidates.append({'id':identity,'name':chosen[0]['name'],'basis':'bearing_only' if bearing_only else 'position_and_motion',
                           'observed_rate_deg_s':actual,'predicted_rate_deg_s':expected,
                           'rate_ratio':actual/expected,'reference_positions':list({p['time']:p for p in chosen}.values()),**score})
    candidates.sort(key=lambda c:c['mean_error_deg'])
    return {'status':'possible_match' if candidates else 'no_match' if evaluated else 'insufficient_reference_coverage',
            'source':'saved_local_adsb','coverage':evidence.get('coverage'),'evaluated_objects':evaluated,
            'candidates':candidates[:5],'candidate_count':len(candidates),
            'limitations':['Bearing-only candidates have no elevation agreement; never use them to discard footage.',
                            'Nearest radio position within two seconds; rate ratio 0.25–4 is an experimental consistency gate, not calibrated confidence.']}
