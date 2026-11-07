"""Bounded summaries of actual observations; never measure across unsupported gaps."""
import math


def describe(rows):
    dt=[b['t']-a['t'] for a,b in zip(rows,rows[1:])]
    distance=[math.hypot(b['x']-a['x'],b['y']-a['y']) for a,b in zip(rows,rows[1:])]
    speed=[d/t for d,t in zip(distance,dt)]
    heading=[math.atan2(b['y']-a['y'],b['x']-a['x']) for a,b in zip(rows,rows[1:])]
    turning=[math.atan2(math.sin(b-a),math.cos(b-a)) for a,b in zip(heading,heading[1:])]
    duration=rows[-1]['t']-rows[0]['t']
    displacement=math.hypot(rows[-1]['x']-rows[0]['x'],rows[-1]['y']-rows[0]['y'])
    return {'sample_count':len(rows),'observed_seconds':duration,'displacement_pixels':displacement,
            'path_pixels':sum(distance),'mean_speed_pixels_per_second':sum(distance)/duration if duration else None,
            'max_speed_pixels_per_second':max(speed,default=None),
            'max_abs_acceleration_pixels_per_second2':max((abs(b-a)/((dt[i]+dt[i+1])/2) for i,(a,b) in enumerate(zip(speed,speed[1:]))),default=None),
            'straightness':displacement/sum(distance) if sum(distance) else None,
            'absolute_turning_radians':sum(abs(t) for t in turning)}


def summarize(event, tracks, cap=256):
    segments=[]
    def append(tid,origin,points):
        current=[]; number=0
        for p in sorted(points,key=lambda p:p['t']):
            if current and p['t']<=current[-1]['t']:continue
            if current and p['t']-current[-1]['t']>.75:
                segments.append({'track_id':tid,'origin':origin,'segment':number,'rows':current});number+=1;current=[]
            current.append(p)
        if current:segments.append({'track_id':tid,'origin':origin,'segment':number,'rows':current})
    detector_groups={}
    invalid_detector_samples=[]
    for index,sample in enumerate(event.get('track_samples',[])):
        tid=sample.get('track_id')
        values=[sample.get(k) for k in ('sample_host_unix','x','y')]
        if not isinstance(tid,str) or not tid or not all(type(v) in (int,float) and math.isfinite(v) for v in values):
            invalid_detector_samples.append(index);continue
        detector_groups.setdefault(tid,[]).append(dict(zip(('t','x','y'),values)))
    for tid,points in detector_groups.items():append(tid,'detector',points)
    for track in tracks:
        append(track['track_id'],'video_recovered',[{'t':p['host_unix'],'x':p['x'],'y':p['y']} for p in track['points']])
    segments.sort(key=lambda s:(-(s['rows'][-1]['t']-s['rows'][0]['t']),s['rows'][0]['t'],s['track_id'],s['origin'],s['segment']))
    kept=[];omitted=[];remaining=cap
    for s in segments:
        cost=min(2,len(s['rows']))
        if cost>remaining:omitted.append({k:v for k,v in s.items() if k!='rows'});continue
        s={**s,'indices':{0,len(s['rows'])-1}};kept.append(s);remaining-=cost
    # Allocate remaining samples fairly among segments, choosing real rows nearest
    # evenly spaced elapsed times, never interpolating synthetic measurements.
    for count in range(3,cap+1):
        for s in kept:
            if remaining<=0:break
            rows=s['rows']
            for i in range(1,count-1):
                target=rows[0]['t']+(rows[-1]['t']-rows[0]['t'])*i/(count-1)
                index=min(range(len(rows)),key=lambda j:abs(rows[j]['t']-target))
                if index not in s['indices']:
                    s['indices'].add(index);remaining-=1;break
        if remaining<=0 or all(len(s['indices'])==len(s['rows']) for s in kept):break
    result=[]
    for s in kept:
        rows=[s['rows'][i] for i in sorted(s['indices'])]
        result.append({k:v for k,v in s.items() if k not in ('rows','indices')} | describe(rows) |
                      {'start_host_unix':rows[0]['t'],'end_host_unix':rows[-1]['t'],
                       'original_sample_count':len(s['rows']),'decimated':len(rows)<len(s['rows'])})
    detector=[s for s in result if s['origin']=='detector']
    return {'detector_only':detector,'extended':result,'omitted_segments':omitted,'partial':bool(omitted or invalid_detector_samples),'invalid_detector_sample_indices':invalid_detector_samples,
            'sample_cap':cap,'sample_count':sum(s['sample_count'] for s in result),
            'timing':'estimated host times; detector and recovered segments remain separate at the mapping boundary',
            'limitations':'Decimation and centroid jitter affect speed/turning. Pixel speed is not physical speed.'}
