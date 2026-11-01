import unittest
from magi.health import search_block_reason
from magi.star_solve import geometry_to_basis_scales, rays_from_points, hints_agree
import numpy as np


class SearchWorkflowTests(unittest.TestCase):
    def test_current_heat_and_throttling_not_historical_flags(self):
        self.assertIsNotNone(search_block_reason({'temperature_c':75}))
        self.assertIsNone(search_block_reason({'temperature_c':74,'throttle_flags':'0x50000'}))
        self.assertIsNotNone(search_block_reason({'throttle_flags':'0x8'}))
        self.assertIsNotNone(search_block_reason({'throttle_flags':'0x1'}))
        self.assertIsNone(search_block_reason({}))

    def test_any_hint_count_constrains_geometry_without_minimum(self):
        geometry=dict(azimuth_deg=180,elevation_deg=45,roll_deg=10,horizontal_fov_deg=90,vertical_fov_deg=60)
        basis,scales=geometry_to_basis_scales(geometry)
        points=np.array([[.3,.3],[.5,.6],[.8,.7]])
        rays=rays_from_points(points,basis,scales)
        hints=[dict(x=float(p[0]),y=float(p[1]),direction=r.tolist()) for p,r in zip(points,rays)]
        for count in (0,1,2,3):self.assertTrue(hints_agree(hints[:count],basis,scales))
        hints[0]['direction']=rays[-1].tolist()
        self.assertFalse(hints_agree(hints,basis,scales))
