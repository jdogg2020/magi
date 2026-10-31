import json
import math
import unittest

from magi.identification import defaults, measure_tracks
from test_motion_identification import matcher, sample_from_ray


class MeasurementTests(unittest.TestCase):
    def event(self, angles=(0, 10, 20), times=(0, 1, 2)):
        settings = {**defaults(), 'latitude':40, 'longitude':-98, 'calibrated':True,
                    'azimuth_deg':0, 'elevation_deg':90, 'horizontal_fov_deg':60, 'vertical_fov_deg':40}
        samples = [sample_from_ray((math.sin(math.radians(a)),0,math.cos(math.radians(a))),1700000000+t)
                   for a,t in zip(angles,times)]
        return {'id':'test','measurement_geometry':{'schema':1,'calibrated':True,
                'geometry':{k:settings[k] for k in ('latitude','longitude','elevation_m','azimuth_deg','elevation_deg','roll_deg','horizontal_fov_deg','vertical_fov_deg')}},
                'track_samples':samples}

    def test_known_path_and_speed(self):
        report=measure_tracks(self.event()); track=report['tracks'][0]
        self.assertEqual(report['status'],'available')
        self.assertAlmostEqual(track['sampled_path_angle_deg'],20)
        self.assertAlmostEqual(track['mean_path_speed_deg_s'],10)
        self.assertIsNone(track['samples'][0]['azimuth_deg'])
        self.assertEqual(len(track['samples'][0]['ray_enu']),3)
        self.assertIn('host_timing_unverified',track['quality_flags'])

    def test_turnaround_separates_path_from_displacement(self):
        track=measure_tracks(self.event((0,10,0)))['tracks'][0]
        self.assertAlmostEqual(track['sampled_path_angle_deg'],20)
        self.assertAlmostEqual(track['net_displacement_deg'],0)

    def test_missing_or_uncalibrated_capture_geometry(self):
        self.assertEqual(measure_tracks({'id':'old'})['status'],'capture_geometry_unavailable')
        event=self.event();event['measurement_geometry']['calibrated']=False
        self.assertEqual(measure_tracks(event)['status'],'calibration_required')

    def test_reference_disabled_and_changed_current_geometry(self):
        event=self.event();obj=matcher(defaults())
        report=obj.classify(event)
        self.assertEqual(report['status'],'disabled')
        self.assertEqual(report['measurements']['status'],'available')
        obj.settings['azimuth_deg']=123
        self.assertEqual(obj.classify(event)['measurements'],report['measurements'])

    def test_duplicate_and_outside_samples(self):
        event=self.event(times=(0,0,2));event['track_samples'].append({**event['track_samples'][-1],'x':999})
        track=measure_tracks(event)['tracks'][0]
        self.assertEqual(track['rejected_sample_count'],2)
        self.assertEqual(track['sample_count'],2)
        self.assertAlmostEqual(track['mean_path_speed_deg_s'],10)
        json.dumps(track,allow_nan=False)

    def test_one_sample_has_no_speed(self):
        track=measure_tracks(self.event((0,),(0,)))['tracks'][0]
        self.assertIsNone(track['mean_path_speed_deg_s'])
        self.assertIsNone(track['max_sample_gap_seconds'])

    def test_sample_limit_preserves_endpoints(self):
        event=self.event([i/100 for i in range(1000)],list(range(1000)))
        track=measure_tracks(event)['tracks'][0]
        self.assertEqual(track['sample_count'],256)
        self.assertEqual(track['duration_seconds'],999)
        self.assertEqual(track['input_valid_sample_count'],1000)
        self.assertIn('sample_limit_applied',track['quality_flags'])

    def test_bearings_wrap_without_long_way_round(self):
        event=self.event((-1,0,1));g=event['measurement_geometry']['geometry']
        g.update(elevation_deg=0,azimuth_deg=359)
        track=measure_tracks(event)['tracks'][0]
        self.assertAlmostEqual(track['sampled_path_angle_deg'],2)
        self.assertAlmostEqual(track['samples'][0]['azimuth_deg'],358)
        self.assertAlmostEqual(track['samples'][-1]['azimuth_deg'],0,places=6)

    def test_star_calibration_fallback_and_invalid_time(self):
        event=self.event();event['camera_calibration']=event.pop('measurement_geometry')
        event['camera_calibration']['fit']={'held_out_max_deg':.2}
        event['track_samples'][0]['host_utc']='invalid'
        report=measure_tracks(event)
        self.assertEqual(report['star_fit']['held_out_max_deg'],.2)
        self.assertEqual(report['tracks'][0]['sample_count'],2)

    def test_malformed_samples_do_not_break_report(self):
        event=self.event()
        event['track_samples'] += [None, {}, {**event['track_samples'][0], 'x':'2'},
            {**event['track_samples'][0], 'track_id':[]}, {**event['track_samples'][0], 'width':True}]
        self.assertEqual(measure_tracks(event)['tracks'][0]['sample_count'],3)
