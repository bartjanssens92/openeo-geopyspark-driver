import datetime

from .base_test_class import BaseTestClass
BaseTestClass.setup_local_spark()
import numpy as np
from geopyspark.geotrellis import (SpaceTimeKey, Tile, _convert_to_unix_time, TemporalProjectedExtent, Extent,
                                   RasterLayer)
from geopyspark.geotrellis.constants import LayerType, CellType
from geopyspark.geotrellis.layer import TiledRasterLayer,Pyramid
from shapely.geometry import Point

from openeogeotrellis.GeotrellisImageCollection import GeotrellisTimeSeriesImageCollection
from openeogeotrellis.numpy_aggregators import *
from unittest import skip, TestCase
from pyspark import SparkContext


class TestMultipleDates(TestCase):
    band1 = np.array([
        [1.0, 1.0, 1.0, 1.0, 1.0],
        [1.0, 1.0, 1.0, 1.0, 1.0],
        [1.0, 1.0, 1.0, 1.0, 1.0],
        [1.0, 1.0, 1.0, 1.0, 1.0],
        [1.0, 1.0, 1.0, 1.0, 1.0]])

    band2 = np.array([
        [2.0, 2.0, 2.0, 2.0, 2.0],
        [2.0, 2.0, 2.0, 2.0, 2.0],
        [2.0, 2.0, 2.0, 2.0, 2.0],
        [2.0, 2.0, 2.0, 2.0, 2.0],
        [2.0, 2.0, 2.0, 2.0, 2.0]])

    tile = Tile.from_numpy_array(band1,no_data_value=-1.0)
    tile2 = Tile.from_numpy_array(band2,no_data_value=-1.0)
    time_1 = datetime.datetime.strptime("2016-08-24T09:00:00Z", '%Y-%m-%dT%H:%M:%SZ')
    time_2 = datetime.datetime.strptime("2017-08-24T09:00:00Z", '%Y-%m-%dT%H:%M:%SZ')
    time_3 = datetime.datetime.strptime("2017-10-17T09:00:00Z", '%Y-%m-%dT%H:%M:%SZ')

    layer = [(SpaceTimeKey(0, 0, time_1), tile),
             (SpaceTimeKey(1, 0, time_1), tile2),
             (SpaceTimeKey(0, 1, time_1), tile),
             (SpaceTimeKey(1, 1, time_1), tile),
             (SpaceTimeKey(0, 0, time_2), tile2),
             (SpaceTimeKey(1, 0, time_2), tile2),
             (SpaceTimeKey(0, 1, time_2), tile2),
             (SpaceTimeKey(1, 1, time_2), tile2),
             (SpaceTimeKey(0, 0, time_3), tile),
             (SpaceTimeKey(1, 0, time_3), tile2),
             (SpaceTimeKey(0, 1, time_3), tile),
             (SpaceTimeKey(1, 1, time_3), tile)
             ]

    rdd = SparkContext.getOrCreate().parallelize(layer)

    extent = {'xmin': 0.0, 'ymin': 0.0, 'xmax': 33.0, 'ymax': 33.0}
    layout = {'layoutCols': 2, 'layoutRows': 2, 'tileCols': 5, 'tileRows': 5}
    metadata = {'cellType': 'float32ud-1.0',
                'extent': extent,
                'crs': '+proj=longlat +datum=WGS84 +no_defs ',
                'bounds': {
                    'minKey': {'col': 0, 'row': 0, 'instant': _convert_to_unix_time(time_1)},
                    'maxKey': {'col': 1, 'row': 1, 'instant': _convert_to_unix_time(time_3)}},
                'layoutDefinition': {
                    'extent': extent,
                    'tileLayout': {'tileCols': 5, 'tileRows': 5, 'layoutCols': 2, 'layoutRows': 2}}}

    tiled_raster_rdd = TiledRasterLayer.from_numpy_rdd(LayerType.SPACETIME, rdd, metadata)

    layer2 = [(TemporalProjectedExtent(Extent(0, 0, 1, 1), epsg=3857, instant=time_1), tile),
              (TemporalProjectedExtent(Extent(1, 0, 2, 1), epsg=3857, instant=time_1), tile),
              (TemporalProjectedExtent(Extent(0, 1, 1, 2), epsg=3857, instant=time_1), tile),
              (TemporalProjectedExtent(Extent(1, 1, 2, 2), epsg=3857, instant=time_1), tile),
              (TemporalProjectedExtent(Extent(1, 0, 2, 1), epsg=3857, instant=time_2), tile),
              (TemporalProjectedExtent(Extent(1, 0, 2, 1), epsg=3857, instant=time_2), tile),
              (TemporalProjectedExtent(Extent(0, 1, 1, 2), epsg=3857, instant=time_2), tile),
              (TemporalProjectedExtent(Extent(1, 1, 2, 2), epsg=3857, instant=time_2), tile),
              (TemporalProjectedExtent(Extent(1, 0, 2, 1), epsg=3857, instant=time_3), tile),
              (TemporalProjectedExtent(Extent(1, 0, 2, 1), epsg=3857, instant=time_3), tile),
              (TemporalProjectedExtent(Extent(0, 1, 1, 2), epsg=3857, instant=time_3), tile),
              (TemporalProjectedExtent(Extent(1, 1, 2, 2), epsg=3857, instant=time_3), tile)]

    rdd2 = SparkContext.getOrCreate().parallelize(layer2)
    raster_rdd = RasterLayer.from_numpy_rdd(LayerType.SPACETIME, rdd2)


    points = [
        Point(1.0, -3.0),
        Point(0.5, 0.5),
        Point(20.0, 3.0),
        Point(1.0, -2.0),
        Point(-10.0, 15.0)
    ]

    def test_max_aggregator(self):
        tiles = [self.tile,self.tile2]
        composite = max_composite(tiles)
        self.assertEqual(2.0, composite.cells[0][0])

    def test_aggregate_max_time(self):

        input = Pyramid( {0:self.tiled_raster_rdd })

        imagecollection = GeotrellisTimeSeriesImageCollection(input)

        stitched = imagecollection.max_time().pyramid.levels[0].stitch()
        print(stitched)
        self.assertEqual(2.0, stitched.cells[0][0][0])

    def test_min_time(self):
        input = Pyramid( {0:self.tiled_raster_rdd })

        imagecollection = GeotrellisTimeSeriesImageCollection(input)
        min_time = imagecollection.min_time()
        max_time = imagecollection.max_time()

        for p in self.points[1:3]:
            result = min_time.timeseries(p.x, p.y,srs="EPSG:3857")
            print(result)
            print(imagecollection.timeseries(p.x,p.y,srs="EPSG:3857"))
            max_result = max_time.timeseries(p.x, p.y,srs="EPSG:3857")
            self.assertEqual(1.0,result['NoDate'])
            self.assertEqual(2.0,max_result['NoDate'])