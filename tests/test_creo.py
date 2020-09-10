import unittest

from openeogeotrellis.catalogs.creo import CatalogClient


class TestCreo(unittest.TestCase):

    def setUp(self):
        self.l2a_catalog = CatalogClient("Sentinel2", "LEVEL2A")

    def test_creo_catalog(self):
        start_date = "2018-04-01"
        end_date = "2018-04-01"

        results = self.l2a_catalog.query_product_paths(start_date, end_date,
                                                       ulx=5, uly=51.1, brx=5.1, bry=51)

        assert 'S2A_MSIL2A_20180401T105031_N0207_R051_T31UFS_20180401T144530.SAFE' in results[0]
