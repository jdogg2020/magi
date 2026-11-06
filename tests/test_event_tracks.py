from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from magi import event_tracks as tracks


def fixture():
    seed = {'track_id': 'bird', 'sample_host_unix': 100., 'x': 10, 'y': 10,
            'box': [9, 9, 12, 12], 'width': 640, 'height': 360}
    event = {'id': 'event-a', 'track_samples': [seed],
             'detection_settings': {'stream': 'substream'},
             'measurement_geometry': {'calibrated': False},
             'segments': [{'host_start_unix':98., 'host_end_unix':102., 'media_start':0., 'media_end':4.}]}
    point = {'origin': 'video_recovered', 'frame_index': 2, 'pts': 1000,
             'host_unix': 99., 'x': 8, 'y': 10, 'box': [7, 9, 10, 12],
             'mapping_status': 'visually_verified_host_estimate', 'excluded_zone': False,
             'quality': {'correlation': .95, 'peak_margin': .2, 'round_trip_error_px': .1}}
    source = {'stream': 'substream', 'file': 'preview-substream.mp4',
              'video_sha256': 'a'*64, 'timeline_sha256': tracks.digest(event['segments']),
              'width': 640, 'height': 360, 'time_base': [1,1000], 'pts_origin': 0}
    payload = {'schema_version': 1, 'event_id': 'event-a', 'camera_id': 'camera-a',
               'evidence_sha256': tracks.evidence_hash(event, 'camera-a'),
               'geometry_sha256': tracks.digest(event['measurement_geometry']),
               'status': 'partial', 'algorithm': 'fixture-only', 'parameters': {},
               'tracks': [{'track_id': 'bird', 'anchor_sample_index': 0, 'anchor_frame_index': 10, 'anchor_pts': 2000, 'source': source,
                           'segment_id': 'continuous-0', 'stop_reason': 'low_confidence',
                           'points': [point]}]}
    return event, payload


class EventTrackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.event, self.payload = fixture()

    def publish(self, payload=None):
        payload = self.payload if payload is None else payload
        (self.root/'substream.json').write_text(json.dumps({'id': self.event['id'], 'segments': self.event['segments']}))
        raw = tracks.canonical_bytes(payload)
        sha = hashlib.sha256(raw).hexdigest()
        (self.root / f'tracks-{sha}.json').write_bytes(raw)
        manifest = {'schema_version': 1, 'event_id': 'event-a', 'camera_id': 'camera-a',
                    'revision': sha, 'sha256': sha, 'file': f'tracks-{sha}.json'}
        (self.root / 'tracks.json').write_text(json.dumps(manifest))
        return sha

    def read(self, **kwargs):
        return tracks.read_tracks(self.root, self.event, camera_id='camera-a', mode='extended', **kwargs)

    def test_detector_only_does_not_touch_sidecars(self):
        with patch.object(tracks.os, 'open', side_effect=AssertionError('filesystem access')):
            result = tracks.read_tracks('not-a-directory', self.event, camera_id='camera-a', mode='detector_only')
        self.assertEqual(result['detector_samples'], self.event['track_samples'])
        self.assertEqual(result['recovered_points'], [])
        result['detector_samples'][0]['x'] = 900
        self.assertEqual(self.event['track_samples'][0]['x'], 10)

    def test_explicit_mode_required(self):
        with self.assertRaises(TypeError): tracks.read_tracks(self.root, self.event, camera_id='camera-a')
        with self.assertRaises(ValueError): tracks.read_tracks(self.root, self.event, camera_id='camera-a', mode='automatic')

    def test_read_valid_revision_preserves_originals(self):
        sha = self.publish()
        before = deepcopy(self.event)
        result = self.read()
        self.assertEqual(result['revision'], sha)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['detector_samples'], before['track_samples'])
        self.assertEqual(result['recovered_points'][0]['origin'], 'video_recovered')
        self.assertEqual(self.event, before)

    def test_missing_revision_falls_back_visibly(self):
        result = self.read()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['detector_samples'], self.event['track_samples'])

    def test_bytes_tamper_rejected(self):
        sha = self.publish()
        with (self.root / f'tracks-{sha}.json').open('ab') as f: f.write(b' ')
        self.assertEqual(self.read()['status'], 'invalid')

    def test_revision_request_cannot_silently_change(self):
        sha = self.publish()
        self.assertEqual(self.read(revision=sha)['status'], 'partial')
        self.assertEqual(self.read(revision='a'*64)['status'], 'invalid')

    def test_annotation_changes_do_not_stale_capture(self):
        self.publish()
        self.event.update(reviewed=True, labels=['bird'], protected=True)
        self.assertEqual(self.read()['status'], 'partial')

    def test_changed_capture_invalidates(self):
        self.publish()
        self.event['track_samples'][0]['x'] = 11
        self.assertEqual(self.read()['status'], 'invalid')

    def test_saved_geometry_changed_is_rejected(self):
        self.publish()
        self.event['measurement_geometry']['calibrated'] = True
        self.assertEqual(self.read()['status'], 'invalid')

    def test_cross_event_and_camera_rejected(self):
        for key in ('event_id', 'camera_id'):
            payload = deepcopy(self.payload); payload[key] = 'other'
            self.publish(payload)
            self.assertEqual(self.read()['status'], 'invalid', key)

    def test_traversal_manifest_and_symlink_rejected(self):
        self.publish()
        p = self.root/'tracks.json'
        manifest = json.loads(p.read_text()); manifest['file'] = '../outside.json'
        p.write_text(json.dumps(manifest))
        self.assertEqual(self.read()['status'], 'invalid')
        sha = self.publish()
        data = self.root/f'tracks-{sha}.json'
        outside = self.root/'outside'; data.rename(outside); data.symlink_to(outside)
        self.assertEqual(self.read()['status'], 'invalid')

    def test_directory_symlink_rejected(self):
        self.publish()
        link = self.root/'link'; link.symlink_to(self.root, target_is_directory=True)
        result = tracks.read_tracks(link, self.event, camera_id='camera-a', mode='extended')
        self.assertEqual(result['status'], 'invalid')

    def test_busy_publication_does_not_block(self):
        self.publish()
        with (self.root/'.tracks-publication.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(self.read()['reason'], 'track publication busy')

    def test_pinned_revision_survives_unlink(self):
        sha = self.publish()
        original = tracks._read
        calls = []
        def unlink_before_data_read(fd):
            calls.append(fd)
            if len(calls) == 3:
                (self.root/f'tracks-{sha}.json').unlink()
            return original(fd)
        with patch.object(tracks, '_read', side_effect=unlink_before_data_read):
            self.assertEqual(self.read()['status'], 'partial')

    def test_reject_invalid_points(self):
        cases = [('origin','detector'), ('host_unix',100.), ('host_unix',101.),
                 ('host_unix',97.), ('pts',200), ('frame_index',10), ('frame_index',True), ('x',-1), ('box',[0,0,9999,9999]),
                 ('mapping_status','unverified'), ('excluded_zone',None)]
        for key, value in cases:
            payload=deepcopy(self.payload); payload['tracks'][0]['points'][0][key]=value
            self.publish(payload)
            self.assertEqual(self.read()['status'], 'invalid', (key,value))

    def test_duplicate_points_and_tracks_rejected(self):
        for kind in ('points','tracks'):
            payload=deepcopy(self.payload)
            if kind == 'points': payload['tracks'][0]['points'] *= 2
            else: payload['tracks'] *= 2
            self.publish(payload)
            self.assertEqual(self.read()['status'], 'invalid')

    def test_only_earliest_anchor(self):
        self.event['track_samples'].append({**self.event['track_samples'][0], 'sample_host_unix':101})
        self.payload['evidence_sha256']=tracks.evidence_hash(self.event,'camera-a')
        self.payload['tracks'][0]['anchor_sample_index']=1
        self.publish()
        self.assertEqual(self.read()['status'], 'invalid')

    def test_meteor_and_cross_stream_not_supported(self):
        self.payload['tracks'][0]['source'].update(stream='mainstream', file='clip.mkv')
        self.publish(); self.assertEqual(self.read()['status'], 'invalid')
        self.event,self.payload=fixture()
        self.event['track_samples'][0]['detection_type']='meteor_candidate'
        self.payload['evidence_sha256']=tracks.evidence_hash(self.event,'camera-a')
        self.publish(); self.assertEqual(self.read()['status'], 'invalid')

    def test_schema_and_size_gates(self):
        self.payload['schema_version']=True
        self.publish(); self.assertEqual(self.read()['status'], 'invalid')
        (self.root/'tracks.json').write_bytes(b' '*(tracks.MAX_BYTES+1))
        self.assertEqual(self.read()['status'], 'invalid')

    def test_duplicate_keys_and_nonfinite_json_rejected(self):
        for raw in (b'{"schema_version":1,"schema_version":1}', b'{"x":NaN}', b'{"x":1e999}'):
            (self.root/'tracks.json').write_bytes(raw)
            self.assertEqual(self.read()['status'], 'invalid')

    def test_arbitrary_point_fields_cannot_override_parent_identity(self):
        self.payload['tracks'][0]['points'][0].update(track_id='other', segment_id='other', source={})
        self.publish()
        point=self.read()['recovered_points'][0]
        self.assertEqual(point['track_id'],'bird')
        self.assertEqual(point['segment_id'],'continuous-0')
        self.assertEqual(point['source']['stream'],'substream')

    def test_stale_substream_timeline_rejected(self):
        self.publish()
        timeline = {'id': 'event-a', 'segments': [{**self.event['segments'][0], 'media_end': 5.}]}
        (self.root/'substream.json').write_text(json.dumps(timeline))
        self.assertEqual(self.read()['status'], 'invalid')

    def test_recovery_cannot_cross_recording_gap(self):
        self.event['segments'] = [
            {'host_start_unix':98., 'host_end_unix':99.5, 'media_start':0., 'media_end':1.5},
            {'host_start_unix':99.8, 'host_end_unix':102., 'media_start':0., 'media_end':2.2}]
        self.payload['evidence_sha256'] = tracks.evidence_hash(self.event, 'camera-a')
        self.payload['tracks'][0]['source']['timeline_sha256'] = tracks.digest(self.event['segments'])
        self.payload['tracks'][0]['anchor_pts'] = 1700
        self.publish()
        self.assertIn('crosses recording gap', self.read()['reason'])

    def test_same_stream_main_timeline(self):
        self.event['detection_settings']['stream'] = 'mainstream'
        self.payload['evidence_sha256'] = tracks.evidence_hash(self.event, 'camera-a')
        self.payload['tracks'][0]['source'].update(stream='mainstream', file='clip.mkv')
        self.publish()
        (self.root/'substream.json').unlink()
        self.assertEqual(self.read()['status'], 'partial')

    def test_mismatched_source_dimensions_rejected(self):
        self.payload['tracks'][0]['source']['width'] = 1280
        self.publish()
        self.assertEqual(self.read()['status'], 'invalid')

    def test_truncation_flag_preserved(self):
        self.event['track_samples_truncated']=True
        self.payload['evidence_sha256']=tracks.evidence_hash(self.event,'camera-a')
        self.publish()
        self.assertTrue(self.read()['detector_samples_truncated'])


if __name__ == '__main__': unittest.main()
