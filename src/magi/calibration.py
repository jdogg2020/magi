"""Local assisted star calibration; no network downloads or blind-solving claims."""
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

import numpy as np


class CalibrationError(ValueError):
    pass


GEOMETRY_KEYS = ('latitude','longitude','elevation_m','azimuth_deg','elevation_deg','roll_deg',
                 'horizontal_fov_deg','vertical_fov_deg')


def source_fingerprint(cfg):
    return hashlib.sha256((cfg.source+'\n'+cfg.analysis_source).encode()).hexdigest()


def active_report(raw, cfg):
    report=raw.get('calibration')
    if (isinstance(report,dict) and report.get('schema')==1
            and report.get('source_fingerprint')==source_fingerprint(cfg)
            and report.get('geometry')=={k:cfg.identification[k] for k in GEOMETRY_KEYS}
            and cfg.identification['calibrated']):
        return report
    return None


def frame_time(meta, media):
    thumbnail=media.get('thumbnail')
    if not isinstance(thumbnail,dict):
        raise CalibrationError('Selected frame has no usable thumbnail metadata.')
    seconds=thumbnail.get('seconds')
    if isinstance(seconds,bool) or not isinstance(seconds,(int,float)) or not math.isfinite(seconds) or seconds<0:
        raise CalibrationError('Selected frame has no usable capture time. Improve its thumbnail first.')
    segments=meta.get('segments')
    if not isinstance(segments,list):
        raise CalibrationError('Selected frame has no usable segment timeline.')
    for segment in segments:
        keys=('media_start','media_end','host_start_unix','host_end_unix')
        if not isinstance(segment,dict) or any(isinstance(segment.get(k),bool) or not isinstance(segment.get(k),(int,float)) or not math.isfinite(segment[k]) for k in keys):
            raise CalibrationError('Selected frame has an invalid segment timeline.')
        duration=segment['media_end']-segment['media_start']
        if duration<=0 or segment['host_end_unix']<=segment['host_start_unix']:
            raise CalibrationError('Selected frame has an invalid segment duration.')
        if 0<=seconds<=duration:
            return segment['host_start_unix']+seconds/duration*(segment['host_end_unix']-segment['host_start_unix'])
        seconds-=duration
    raise CalibrationError('Selected frame is outside its recorded segment timeline.')


def load_catalog():
    return json.loads((Path(__file__).parent/'data'/'bright-stars.json').read_text())


def visible_stars(timestamp, latitude, longitude, elevation):
    try:
        from skyfield.api import load, wgs84
        from skyfield.positionlib import position_of_radec
    except ImportError:
        raise CalibrationError('Star calibration needs the astronomy packages. Install with scripts/install.sh --identification.') from None
    catalog=load_catalog()
    t=load.timescale(builtin=True).from_datetime(datetime.fromtimestamp(timestamp,timezone.utc))
    stars=catalog['stars']
    ra=np.array([s['ra_deg']/15 for s in stars]);dec=np.array([s['dec_deg'] for s in stars])
    # Skyfield's horizon frame is north,east,up. Magi stores east,north,up.
    xyz=position_of_radec(ra,dec,t=t).frame_xyz(wgs84.latlon(latitude,longitude,elevation_m=elevation)).au
    enu=xyz[[1,0,2]].T;enu/=np.linalg.norm(enu,axis=1)[:,None]
    result=[]
    for star,ray in zip(stars,enu):
        alt=math.degrees(math.asin(float(ray[2])))
        if alt>=10:
            result.append({**star,'altitude_deg':round(alt,3),
                'azimuth_deg':round(math.degrees(math.atan2(ray[0],ray[1]))%360,3),
                'direction':ray.tolist()})
    return result


def _cross(v):
    x,y,z=v
    return np.array([[0,-z,y],[z,0,-x],[-y,x,0]])


def _rays(points,basis,scales):
    xy=np.column_stack((2*points[:,0]-1,1-2*points[:,1]))*scales
    rays=np.column_stack((xy,np.ones(len(points))))@basis.T
    return rays/np.linalg.norm(rays,axis=1)[:,None]


def _step(basis,scales,step):
    rotation=step[:3];angle=np.linalg.norm(rotation);matrix=_cross(rotation)
    rotate=np.eye(3)+matrix if angle<1e-10 else np.eye(3)+math.sin(angle)/angle*matrix+(1-math.cos(angle))/angle**2*(matrix@matrix)
    return rotate@basis, scales*np.exp(np.clip(step[3:],-.5,.5))


def _fit(points,directions):
    coords=np.column_stack((2*points[:,0]-1,1-2*points[:,1],np.ones(len(points))))
    rows=np.concatenate([np.kron(_cross(ray),coord.reshape(1,3)) for coord,ray in zip(coords,directions)])
    _,_,v=np.linalg.svd(rows,full_matrices=False);matrix=v[-1].reshape(3,3)
    if np.sum((coords@matrix.T)*directions)<0:matrix=-matrix
    if np.linalg.norm(matrix[:,2])<1e-10:
        raise CalibrationError('Star geometry is degenerate. Use a wider spread of stars.')
    forward=matrix[:,2]/np.linalg.norm(matrix[:,2]);right=matrix[:,0]-forward*np.dot(matrix[:,0],forward)
    if np.linalg.norm(right)<1e-10:
        raise CalibrationError('Star geometry is degenerate. Use a wider spread of stars.')
    right/=np.linalg.norm(right);up=np.cross(right,forward)
    if np.dot(up,matrix[:,1])<=0:raise CalibrationError('Star layout is mirrored or misidentified. Check the selected stars.')
    basis=np.column_stack((right,up,forward))
    scales=np.array([np.linalg.norm(matrix[:,i])/np.linalg.norm(matrix[:,2]) for i in (0,1)])
    damping=.001
    for _ in range(60):
        residual=(_rays(points,basis,scales)-directions).ravel()
        columns=[]
        for j in range(5):
            delta=np.zeros(5);delta[j]=1e-5;b,s=_step(basis,scales,delta)
            columns.append(((_rays(points,b,s)-directions).ravel()-residual)/1e-5)
        jac=np.array(columns).T
        step=np.linalg.solve(jac.T@jac+damping*np.diag(np.sum(jac*jac,axis=0)+1e-8),-jac.T@residual)
        if np.linalg.norm(step[:3])>.3:step*=.3/np.linalg.norm(step[:3])
        b,s=_step(basis,scales,step)
        if not np.all((s>math.tan(math.radians(.5)))&(s<math.tan(math.radians(85)))):
            damping*=10;continue
        new=(_rays(points,b,s)-directions).ravel()
        if np.dot(new,new)<np.dot(residual,residual):
            basis,scales=b,s;damping=max(1e-8,damping/3)
            if np.linalg.norm(step)<1e-8:break
        else:damping*=10
    singular=np.linalg.svd(jac,compute_uv=False)
    if singular[-1]/singular[0]<1e-5:raise CalibrationError('Star layout does not constrain the camera well enough. Spread stars across the image.')
    return basis,scales


def _errors(predicted,actual):
    return np.degrees(np.arccos(np.clip(np.sum(predicted*actual,axis=1),-1,1)))


def solve_geometry(points,directions):
    points=np.asarray(points,dtype=float);directions=np.asarray(directions,dtype=float)
    if (points.ndim!=2 or points.shape[1]!=2 or not 6<=len(points)<=20 or directions.shape!=(len(points),3)
            or not np.isfinite(points).all() or not np.isfinite(directions).all()
            or np.any(points<0) or np.any(points>1) or np.any(np.linalg.norm(directions,axis=1)<.9) or np.any(np.linalg.norm(directions,axis=1)>1.1)):
        raise CalibrationError('Select 6–20 different named stars on the image.')
    if np.ptp(points[:,0])<.35 or np.ptp(points[:,1])<.25 or np.linalg.eigvalsh(np.cov(points.T))[0]<.002:
        raise CalibrationError('Spread the stars across both image axes; a small cluster or line cannot calibrate the lens.')
    directions=directions/np.linalg.norm(directions,axis=1)[:,None]
    try:
        basis,scales=_fit(points,directions)
        errors=_errors(_rays(points,basis,scales),directions)
        holdouts=[]
        for i in range(len(points)):
            keep=np.arange(len(points))!=i;b,s=_fit(points[keep],directions[keep])
            holdouts.append(float(_errors(_rays(points[i:i+1],b,s),directions[i:i+1])[0]))
    except (np.linalg.LinAlgError,FloatingPointError,ZeroDivisionError):
        raise CalibrationError('The star fit is unstable. Check identities and use a wider spread.') from None
    rms=float(np.sqrt(np.mean(errors**2)));maximum=float(np.max(errors));check=max(holdouts)
    if rms>.2 or maximum>.4 or check>.5:
        raise CalibrationError(f'Star fit rejected: RMS {rms:.3f}°, maximum {maximum:.3f}°, held-out check {check:.3f}°. Check names, time, location and lens distortion.')
    right,forward=basis[:,0],basis[:,2]
    az=math.atan2(forward[0],forward[1]) if math.hypot(forward[0],forward[1])>1e-7 else 0
    el=math.asin(float(np.clip(forward[2],-1,1)))
    reference_right=np.array([math.cos(az),-math.sin(az),0])
    reference_up=np.array([-math.sin(el)*math.sin(az),-math.sin(el)*math.cos(az),math.cos(el)])
    roll=math.atan2(-np.dot(right,reference_up),np.dot(right,reference_right))
    geometry={'azimuth_deg':math.degrees(az)%360,'elevation_deg':math.degrees(el),'roll_deg':math.degrees(roll),
        'horizontal_fov_deg':math.degrees(2*math.atan(scales[0])), 'vertical_fov_deg':math.degrees(2*math.atan(scales[1]))}
    camera=directions@basis
    projected=np.column_stack(((camera[:,0]/camera[:,2]/scales[0]+1)/2,(1-camera[:,1]/camera[:,2]/scales[1])/2))
    return {'projected_matches':projected.tolist(),'geometry':geometry,'rms_deg':rms,'max_error_deg':maximum,'held_out_max_deg':check,
        'uncertainty_floor_deg':max(.1,check),'residuals_deg':errors.tolist(),
        'model':'rectilinear pinhole; fixed image center; no lens-distortion fit',
        'assumptions':['Named stars selected by operator; not blind recognition',
            'J2000 catalog with Skyfield Earth rotation/precession/nutation; no proper motion, aberration or refraction',
            'Estimated host capture time and matching main/substream field of view',
            'Wrong location or time can still produce a small residual; this fit cannot independently verify either',
            'Near zenith, azimuth and roll are coupled; individual angles are not uniquely determined',
            'Residuals describe selected stars, not a guarantee across the whole lens']}
