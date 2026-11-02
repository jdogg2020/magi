"""Normalized exclusion geometry and cached, outward feather weights."""
import math
import numpy as np


def points(zone):
    if 'points' in zone:
        return zone['points']
    x, y, w, h = (zone[k] for k in ('x', 'y', 'width', 'height'))
    return [[x, y], [x+w, y], [x+w, y+h], [x, y+h]]


def validate_zone(zone):
    if not isinstance(zone, dict):
        raise ValueError('invalid ignore zone')
    keys = set(zone) - {'feather'}
    def number(v):
        return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)
    if 'feather' in zone and (not number(zone['feather']) or not 0 <= zone['feather'] <= .1):
        raise ValueError('mask feather must be between 0 and 10% of image height or width')
    if keys == {'x', 'y', 'width', 'height'}:
        if not all(number(zone[k]) for k in keys):
            raise ValueError('invalid ignore zone coordinate')
        x, y, w, h = (zone[k] for k in ('x', 'y', 'width', 'height'))
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x+w > 1 or y+h > 1:
            raise ValueError('ignore zone must fit within the image')
        result = {k: float(zone[k]) for k in keys}
    elif keys == {'points'}:
        p = zone['points']
        if not isinstance(p, list) or not 3 <= len(p) <= 64:
            raise ValueError('polygon needs 3 to 64 points')
        if any(not isinstance(v, (list, tuple)) or len(v) != 2 or
               any(not number(c) or not 0 <= c <= 1 for c in v) for v in p):
            raise ValueError('invalid polygon coordinate')
        def cross(a,b,c):
            return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
        edges = list(zip(p, p[1:]+p[:1]))
        if any(a == b for a,b in edges) or abs(sum(a[0]*b[1]-b[0]*a[1] for a,b in edges)) < 1e-10:
            raise ValueError('polygon must have positive area and distinct corners')
        def on(a,b,c):
            return abs(cross(a,b,c)) < 1e-12 and all(min(a[k],b[k])-1e-12 <= c[k] <= max(a[k],b[k])+1e-12 for k in (0,1))
        for i,(a,b) in enumerate(edges):
            for j,(c,d) in enumerate(edges):
                if j <= i+1 or (i == 0 and j == len(edges)-1):
                    continue
                if (cross(a,b,c)*cross(a,b,d)<0 and cross(c,d,a)*cross(c,d,b)<0) or on(a,b,c) or on(a,b,d) or on(c,d,a) or on(c,d,b):
                    raise ValueError('polygon edges must not cross or touch')
        result = {'points': [[float(x),float(y)] for x,y in p]}
    else:
        raise ValueError('invalid ignore zone')
    if 'feather' in zone:
        result['feather'] = float(zone['feather'])
    return result


def weights(zones, shape):
    """Compute once per geometry; no new dependency or per-frame distance work.

    Rectangles retain their historical half-open pixel-center rule. Polygon
    boundaries are excluded. Feather is Euclidean distance outside the actual
    shape, measured as a fraction of the shorter frame dimension.
    """
    height, width = shape
    # Float64 preserves historical rectangle comparisons at exact boundaries.
    yy, xx = np.mgrid[:height, :width].astype(np.float64)
    xx += .5; yy += .5
    active = np.ones(shape, dtype=bool)
    weight = np.ones(shape, dtype=np.float32)
    for zone in zones:
        if 'points' not in zone and not zone.get('feather', 0):
            x,y,w,h = (zone[k] for k in ('x','y','width','height'))
            active &= ~((xx/width>=x)&(xx/width<x+w)&(yy/height>=y)&(yy/height<y+h))
            continue
        p = np.asarray(points(zone)) * [width, height]
        inside = np.zeros(shape, dtype=bool)
        distance2 = np.full(shape, np.inf, dtype=np.float32)
        for a,b in zip(p, np.roll(p,-1,axis=0)):
            dx, dy = b-a
            t = np.clip(((xx-a[0])*dx+(yy-a[1])*dy)/(dx*dx+dy*dy),0,1)
            distance2 = np.minimum(distance2,(xx-a[0]-t*dx)**2+(yy-a[1]-t*dy)**2)
            if dy:
                inside ^= ((a[1]>yy)!=(b[1]>yy)) & (xx < a[0]+(yy-a[1])*dx/dy)
        if 'points' in zone:
            inside |= distance2 < 1e-10
        else:
            x,y,w,h = (zone[k] for k in ('x','y','width','height'))
            inside = (xx/width>=x)&(xx/width<x+w)&(yy/height>=y)&(yy/height<y+h)
        active &= ~inside
        feather = zone.get('feather',0)*min(width,height)
        if feather:
            weight = np.minimum(weight,np.sqrt(distance2)/feather)
    weight[~active] = 0
    return active, weight
