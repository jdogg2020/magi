"""Remove burned-in camera overlays using read/modify/write and readback."""
import copy

from .camera_info import CameraInfoError, camera_api


def disable_overlays(camera):
    with camera_api(camera) as (send, _token):
        def command(name, param):
            result = send([{'cmd': name, 'action': 0, 'param': param}])
            if (not isinstance(result, list) or len(result) != 1 or
                    not isinstance(result[0], dict) or result[0].get('cmd') != name or
                    result[0].get('code') != 0):
                raise CameraInfoError(f'Camera overlay command {name} failed or is unsupported.')
            return result[0]

        def read():
            osd = command('GetOsd', {'channel': 0}).get('value', {}).get('Osd')
            if (not isinstance(osd, dict) or osd.get('channel') != 0 or
                    any(not isinstance(osd.get(key), dict) or
                        type(osd[key].get('enable')) is not int or
                        osd[key]['enable'] not in (0, 1) for key in ('osdChannel', 'osdTime')) or
                    type(osd.get('watermark')) is not int or osd['watermark'] not in (0, 1)):
                raise CameraInfoError('Camera did not report all three overlay controls; removal cannot be verified.')
            return osd

        original = read()
        desired = copy.deepcopy(original)
        desired['osdChannel']['enable'] = 0
        desired['osdTime']['enable'] = 0
        desired['watermark'] = 0
        if desired != original:
            response = command('SetOsd', {'Osd': desired})
            if response.get('value', {}).get('rspCode') != 200:
                raise CameraInfoError('Camera did not acknowledge overlay removal. Inspect again before retrying.')
            confirmed = read()
            if any(confirmed[key]['enable'] != 0 for key in ('osdChannel', 'osdTime')) or confirmed['watermark'] != 0:
                raise CameraInfoError('Camera did not confirm watermark, timestamp and camera name are all disabled.')
