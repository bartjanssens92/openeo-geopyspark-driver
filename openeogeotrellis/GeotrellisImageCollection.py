import functools
import json
import logging
import math
import os
import pathlib
import subprocess
import tempfile
import uuid
from datetime import datetime, date
from typing import Dict, List, Union, Tuple, Iterable, Callable

import geopyspark as gps
import numpy as np
import pandas as pd
import pyproj
import pytz
from geopyspark import TiledRasterLayer, TMS, Pyramid, Tile, SpaceTimeKey, SpatialKey, Metadata
from geopyspark.geotrellis import Extent, ResampleMethod
from geopyspark.geotrellis.constants import CellType
from openeo.internal.process_graph_visitor import ProcessGraphVisitor
from openeo_driver.backend import ServiceMetadata
from openeo_driver.delayed_vector import DelayedVector
from openeo_driver.errors import FeatureUnsupportedException, OpenEOApiException, InternalException
from openeogeotrellis.geotrellis_tile_processgraph_visitor import GeotrellisTileProcessGraphVisitor
from openeogeotrellis.run_udf import run_user_code
from py4j.java_gateway import JVMView

try:
    from openeo_udf.api.base import UdfData, SpatialExtent

except ImportError as e:
    from openeo_udf.api.udf_data import UdfData
    from openeo_udf.api.spatial_extent import SpatialExtent

from pandas import Series
from shapely.geometry import Point, Polygon, MultiPolygon, GeometryCollection
import xarray as xr

from openeo.imagecollection import ImageCollection
import openeo.metadata
from openeo.metadata import CollectionMetadata, Band
from openeo_driver.save_result import AggregatePolygonResult
from openeogeotrellis.configparams import ConfigParams
from openeogeotrellis.service_registry import SecondaryService, AbstractServiceRegistry
from openeogeotrellis.utils import to_projected_polygons,log_memory


_log = logging.getLogger(__name__)


class GeotrellisTimeSeriesImageCollection(ImageCollection):

    # TODO: no longer dependent on ServiceRegistry so it can be removed
    def __init__(self, pyramid: Pyramid, service_registry: AbstractServiceRegistry, metadata: CollectionMetadata = None):
        super().__init__(metadata=metadata)
        self.pyramid = pyramid
        self.tms = None
        self._service_registry = service_registry

    def _get_jvm(self) -> JVMView:
        # TODO: cache this?
        return gps.get_spark_context()._gateway.jvm

    def _is_spatial(self):
        return self.pyramid.levels[self.pyramid.max_zoom].layer_type == gps.LayerType.SPATIAL

    def apply_to_levels(self, func):
        """
        Applies a function to each level of the pyramid. The argument provided to the function is of type TiledRasterLayer

        :param func:
        :return:
        """
        pyramid = Pyramid({k:func( l ) for k,l in self.pyramid.levels.items()})
        return GeotrellisTimeSeriesImageCollection(pyramid, self._service_registry, metadata=self.metadata)

    def _create_tilelayer(self,contextrdd, layer_type, zoom_level):
        jvm = self._get_jvm()
        spatial_tiled_raster_layer = jvm.geopyspark.geotrellis.SpatialTiledRasterLayer
        temporal_tiled_raster_layer = jvm.geopyspark.geotrellis.TemporalTiledRasterLayer

        if layer_type == gps.LayerType.SPATIAL:
            srdd = spatial_tiled_raster_layer.apply(jvm.scala.Option.apply(zoom_level),contextrdd)
        else:
            srdd = temporal_tiled_raster_layer.apply(jvm.scala.Option.apply(zoom_level),contextrdd)

        return gps.TiledRasterLayer(layer_type, srdd)

    def _apply_to_levels_geotrellis_rdd(self, func):
        """
        Applies a function to each level of the pyramid. The argument provided to the function is the Geotrellis ContextRDD.

        :param func:
        :return:
        """
        pyramid = Pyramid({k:self._create_tilelayer(func( l.srdd.rdd(),k ),l.layer_type,k) for k,l in self.pyramid.levels.items()})
        return GeotrellisTimeSeriesImageCollection(pyramid, self._service_registry, metadata=self.metadata)

    def band_filter(self, bands) -> 'ImageCollection':
        return self.apply_to_levels(lambda rdd: rdd.bands(bands))

    def _data_source_type(self):
        return self.metadata.get("_vito", "data_source", "type", default="Accumulo")

    def date_range_filter(self, start_date: Union[str, datetime, date],end_date: Union[str, datetime, date]) -> 'ImageCollection':
        return self.apply_to_levels(lambda rdd: rdd.filter_by_times([pd.to_datetime(start_date),pd.to_datetime(end_date)]))

    def filter_bbox(self, west, east, north, south, crs=None, base=None, height=None) -> 'ImageCollection':
        # Note: the bbox is already extracted in `apply_process` and applied in `GeoPySparkLayerCatalog.load_collection` through the viewingParameters
        return self

    def rename_dimension(self, source:str, target:str):
        return GeotrellisTimeSeriesImageCollection(self.pyramid,self._service_registry,self.metadata.rename_dimension(source,target))

    def apply(self, process: str, arguments: dict={}) -> 'ImageCollection':
        from openeogeotrellis.backend import SingleNodeUDFProcessGraphVisitor, GeoPySparkBackendImplementation
        if isinstance(process, dict):
            apply_callback = GeoPySparkBackendImplementation.accept_process_graph(process)
            #apply should leave metadata intact, so can do a simple call?
            return self.reduce_bands(apply_callback)

        result_collection = None
        if isinstance(process, SingleNodeUDFProcessGraphVisitor):
            udf = process.udf_args.get('udf', None)
            context = process.udf_args.get('context', {})
            if not isinstance(udf, str):
                raise ValueError(
                    "The 'run_udf' process requires at least a 'udf' string argument, but got: '%s'." % udf)
            self.apply_tiles(udf,context)

        if isinstance(process,str):
            #old 04x code path
            if 'y' in arguments:
                raise NotImplementedError("Apply only supports unary operators,"
                                          " but got {p!r} with {a!r}".format(p=process, a=arguments))
            applyProcess = gps.get_spark_context()._jvm.org.openeo.geotrellis.OpenEOProcesses().applyProcess
            return self._apply_to_levels_geotrellis_rdd(lambda rdd, k: applyProcess(rdd, process))

    def reduce(self, reducer: str, dimension: str) -> 'ImageCollection':
        # TODO: rename this to reduce_temporal (because it only supports temporal reduce)?
        from .numpy_aggregators import var_composite, std_composite, min_composite, max_composite, sum_composite

        reducer = self._normalize_temporal_reducer(dimension, reducer)

        if reducer == 'Variance':
            return self._aggregate_over_time_numpy(var_composite)
        elif reducer == 'StandardDeviation':
            return self._aggregate_over_time_numpy(std_composite)
        elif reducer == 'Min':
            return self._aggregate_over_time_numpy(min_composite)
        elif reducer == 'Max':
            return self._aggregate_over_time_numpy(max_composite)
        elif reducer == 'Sum':
            return self._aggregate_over_time_numpy(sum_composite)
        else:
            return self.apply_to_levels(lambda layer: layer.to_spatial_layer().aggregate_by_cell(reducer))

    def reduce_bands(self, pgVisitor: GeotrellisTileProcessGraphVisitor) -> 'GeotrellisTimeSeriesImageCollection':
        """
        TODO Define in super class? API is not yet ready for client side...
        :param pgVisitor:
        :return:
        """
        pysc = gps.get_spark_context()
        float_datacube = self.apply_to_levels(lambda layer : layer.convert_data_type("float32"))
        result = float_datacube._apply_to_levels_geotrellis_rdd(
            lambda rdd, level: pysc._jvm.org.openeo.geotrellis.OpenEOProcesses().mapBands(rdd, pgVisitor.builder))
        return result

    def _normalize_temporal_reducer(self, dimension: str, reducer: str) -> str:
        if dimension != self.metadata.temporal_dimension.name:
            raise FeatureUnsupportedException('Reduce on dimension {d!r} not supported'.format(d=dimension))
        if reducer.upper() in ["MIN", "MAX", "SUM", "MEAN", "VARIANCE"]:
            reducer = reducer.lower().capitalize()
        elif reducer.upper() == "SD":
            reducer = "StandardDeviation"
        else:
            raise FeatureUnsupportedException('Reducer {r!r} not supported'.format(r=reducer))
        return reducer

    def add_dimension(self, name: str, label: str, type: str = None):
        return GeotrellisTimeSeriesImageCollection(
            pyramid=self.pyramid, service_registry=self._service_registry,
            metadata=self.metadata.add_dimension(name=name, label=label, type=type)
        )

    @classmethod
    def _mapTransform(cls, layoutDefinition, spatialKey):
        ex = layoutDefinition.extent
        x_range = ex.xmax - ex.xmin
        xinc = x_range / layoutDefinition.tileLayout.layoutCols
        yrange = ex.ymax - ex.ymin
        yinc = yrange / layoutDefinition.tileLayout.layoutRows
        return SpatialExtent(
            top=ex.ymax - yinc * spatialKey.row,
            bottom=ex.ymax - yinc * (spatialKey.row + 1),
            right=ex.xmin + xinc * (spatialKey.col + 1),
            left=ex.xmin + xinc * spatialKey.col,
            height=layoutDefinition.tileLayout.tileCols,
            width=layoutDefinition.tileLayout.tileRows
        )

    @classmethod
    def _tile_to_datacube(cls, bands_numpy: np.ndarray, extent: SpatialExtent,
                          band_dimension: openeo.metadata.BandDimension, start_times=None):
        from openeo_udf.api.datacube import DataCube
        coords = {}
        dims = ('bands','x', 'y')
        if len(bands_numpy.shape) == 4:
            #we have a temporal dimension
            coords = {'t':start_times}
            dims = ('t' ,'bands','x', 'y')
        if band_dimension:
            # TODO: also use the band dimension name (`band_dimension.name`) instead of hardcoded "bands"?
            coords['bands'] = band_dimension.band_names
        the_array = xr.DataArray(bands_numpy, coords=coords,dims=dims,name="openEODataChunk")
        return DataCube(the_array)


    def apply_tiles_spatiotemporal(self,function,context={}) -> ImageCollection:
        """
        Apply a function to a group of tiles with the same spatial key.
        :param function:
        :return:
        """

        #early compile to detect syntax errors
        compiled_code = compile(function,'UDF.py',mode='exec')

        def tilefunction(metadata:Metadata, openeo_metadata: CollectionMetadata, tiles:Tuple[gps.SpatialKey, List[Tuple[SpaceTimeKey, Tile]]]):
            tile_list = list(tiles[1])
            #sort by instant
            tile_list.sort(key=lambda tup: tup[0].instant)
            dates = map(lambda t: t[0].instant, tile_list)
            arrays = map(lambda t: t[1].cells, tile_list)
            multidim_array = np.array(list(arrays))

            extent = GeotrellisTimeSeriesImageCollection._mapTransform(metadata.layout_definition,tile_list[0][0])

            from openeo_udf.api.datacube import DataCube
            #new UDF API available

            datacube:DataCube = GeotrellisTimeSeriesImageCollection._tile_to_datacube(
                multidim_array,
                extent=extent,
                band_dimension=openeo_metadata.band_dimension if openeo_metadata.has_band_dimension() else None,
                start_times=pd.DatetimeIndex(dates)
            )

            data = UdfData({"EPSG": 900913}, [datacube])
            data.user_context = context

            result_data = run_user_code(function,data)
            cubes = result_data.get_datacube_list()
            if len(cubes)!=1:
                raise ValueError("The provided UDF should return one datacube, but got: "+ str(cubes))
            result_array:xr.DataArray = cubes[0].array
            if 't' in result_array.dims:
                return [(SpaceTimeKey(col=tiles[0].col, row=tiles[0].row,instant=pd.Timestamp(timestamp)),
                  Tile(array_slice.values, CellType.FLOAT32, tile_list[0][1].no_data_value)) for timestamp, array_slice in result_array.groupby('t')]
            else:
                return [(SpaceTimeKey(col=tiles[0].col, row=tiles[0].row,instant=datetime.now()),
                  Tile(result_array.values, CellType.FLOAT32, tile_list[0][1].no_data_value))]

        def rdd_function(openeo_metadata: CollectionMetadata, rdd):
            floatrdd = rdd.convert_data_type(CellType.FLOAT32).to_numpy_rdd()
            grouped_by_spatial_key = floatrdd.map(lambda t: (gps.SpatialKey(t[0].col, t[0].row), (t[0], t[1]))).groupByKey()

            return gps.TiledRasterLayer.from_numpy_rdd(gps.LayerType.SPACETIME,
                                                       grouped_by_spatial_key.flatMap(
                                                    log_memory(partial(tilefunction, rdd.layer_metadata, openeo_metadata))),
                                                       rdd.layer_metadata)
        from functools import partial
        return self.apply_to_levels(partial(rdd_function, self.metadata))



    def reduce_dimension(self, dimension: str, reducer:Union[ProcessGraphVisitor,Dict],binary=False, context=None) -> 'ImageCollection':
        from openeogeotrellis.backend import SingleNodeUDFProcessGraphVisitor,GeoPySparkBackendImplementation
        if isinstance(reducer,dict):
            reducer = GeoPySparkBackendImplementation.accept_process_graph(reducer)

        result_collection = None
        if isinstance(reducer,SingleNodeUDFProcessGraphVisitor):
            udf = reducer.udf_args.get('udf',None)
            context = reducer.udf_args.get('context', {})
            if not isinstance(udf,str):
                raise ValueError("The 'run_udf' process requires at least a 'udf' string argument, but got: '%s'."%udf)
            if dimension == self.metadata.temporal_dimension.name:
                #EP-2760 a special case of reduce where only a single udf based callback is provided. The more generic case is not yet supported.
                result_collection = self.apply_tiles_spatiotemporal(udf,context)
            elif dimension == self.metadata.band_dimension.name:
                result_collection = self.apply_tiles(udf,context)

        elif self.metadata.has_band_dimension() and dimension == self.metadata.band_dimension.name:
            result_collection = self.reduce_bands(reducer)
        elif hasattr(reducer,'processes') and isinstance(reducer.processes,dict) and len(reducer.processes) == 1:
            result_collection = self.reduce(reducer.processes.popitem()[0],dimension)
        else:
            raise ValueError("Unsupported combination of reducer %s and dimension %s."%(reducer,dimension))
        if result_collection is not None:
            result_collection.metadata = result_collection.metadata.reduce_dimension(dimension)
            if self.metadata.has_temporal_dimension() and dimension == self.metadata.temporal_dimension.name and self.pyramid.layer_type != gps.LayerType.SPATIAL:
                result_collection = result_collection.apply_to_levels(lambda rdd:  rdd.to_spatial_layer() if rdd.layer_type != gps.LayerType.SPATIAL else rdd)
        return result_collection


    def apply_tiles(self, function,context={}) -> 'ImageCollection':
        """Apply a function to the given set of bands in this image collection."""
        #TODO apply .bands(bands)

        def tilefunction(metadata: Metadata, openeo_metadata: CollectionMetadata,
                         geotrellis_tile: Tuple[SpaceTimeKey, Tile]):

            key = geotrellis_tile[0]
            extent = GeotrellisTimeSeriesImageCollection._mapTransform(metadata.layout_definition,key)

            from openeo_udf.api.datacube import DataCube

            datacube:DataCube = GeotrellisTimeSeriesImageCollection._tile_to_datacube(
                geotrellis_tile[1].cells,
                extent=extent,
                band_dimension=openeo_metadata.band_dimension
            )

            data = UdfData({"EPSG": 900913}, [datacube])
            data.user_context = context

            result_data = run_user_code(function,data)
            cubes = result_data.get_datacube_list()
            if len(cubes)!=1:
                raise ValueError("The provided UDF should return one datacube, but got: "+ str(cubes))
            result_array:xr.DataArray = cubes[0].array
            print(result_array.dims)
            return (key,Tile(result_array.values, geotrellis_tile[1].cell_type,geotrellis_tile[1].no_data_value))


        def rdd_function(openeo_metadata: CollectionMetadata, rdd):
            return gps.TiledRasterLayer.from_numpy_rdd(rdd.layer_type,
                                                rdd.convert_data_type(CellType.FLOAT32).to_numpy_rdd().map(
                                                    log_memory(partial(tilefunction, rdd.layer_metadata, openeo_metadata))),
                                                rdd.layer_metadata)
        from functools import partial
        return self.apply_to_levels(partial(rdd_function, self.metadata))

    def aggregate_time(self, temporal_window, aggregationfunction) -> Series :
        #group keys
        #reduce
        pass

    def aggregate_temporal(self, intervals: List, labels: List, reducer, dimension: str = None) -> 'ImageCollection':
        """ Computes a temporal aggregation based on an array of date and/or time intervals.

            Calendar hierarchies such as year, month, week etc. must be transformed into specific intervals by the clients. For each interval, all data along the dimension will be passed through the reducer. The computed values will be projected to the labels, so the number of labels and the number of intervals need to be equal.

            If the dimension is not set, the data cube is expected to only have one temporal dimension.

            :param intervals: Temporal left-closed intervals so that the start time is contained, but not the end time.
            :param labels: Labels for the intervals. The number of labels and the number of groups need to be equal.
            :param reducer: A reducer to be applied on all values along the specified dimension. The reducer must be a callable process (or a set processes) that accepts an array and computes a single return value of the same type as the input values, for example median.
            :param dimension: The temporal dimension for aggregation. All data along the dimension will be passed through the specified reducer. If the dimension is not set, the data cube is expected to only have one temporal dimension.

            :return: An ImageCollection containing  a result for each time window
        """
        intervals_iso = list(map(lambda d:pd.to_datetime(d).strftime('%Y-%m-%dT%H:%M:%SZ'),intervals))
        labels_iso = list(map(lambda l:pd.to_datetime(l).strftime('%Y-%m-%dT%H:%M:%SZ'), labels))
        pysc = gps.get_spark_context()
        mapped_keys = self._apply_to_levels_geotrellis_rdd(
            lambda rdd,level: pysc._jvm.org.openeo.geotrellis.OpenEOProcesses().mapInstantToInterval(rdd,intervals_iso,labels_iso))
        reducer = self._normalize_temporal_reducer(dimension, reducer)
        return mapped_keys.apply_to_levels(lambda rdd: rdd.aggregate_by_cell(reducer))

    def _aggregate_over_time_numpy(self, reducer: Callable[[Iterable[Tile]], Tile]) -> 'ImageCollection':
        """
        Aggregate over time.
        :param reducer: a function that reduces n Tiles to a single Tile
        :return:
        """
        def aggregate_temporally(layer):
            grouped_numpy_rdd = layer.to_spatial_layer().convert_data_type(CellType.FLOAT32).to_numpy_rdd().groupByKey()

            composite = grouped_numpy_rdd.mapValues(reducer)
            aggregated_layer = TiledRasterLayer.from_numpy_rdd(gps.LayerType.SPATIAL, composite, layer.layer_metadata)
            return aggregated_layer

        return self.apply_to_levels(aggregate_temporally)


    @classmethod
    def __reproject_polygon(cls, polygon: Union[Polygon, MultiPolygon], srs, dest_srs):
        from shapely.ops import transform

        project = functools.partial(
            pyproj.transform,
            pyproj.Proj(srs),  # source coordinate system
            pyproj.Proj(dest_srs))  # destination coordinate system

        return transform(project, polygon)  # apply projection

    def merge(self,other:'GeotrellisTimeSeriesImageCollection',overlaps_resolver:str=None):
        #we may need to align datacubes automatically?
        #other_pyramid_levels = {k: l.tile_to_layout(layout=self.pyramid.levels[k]) for k, l in other.pyramid.levels.items()}
        pysc = gps.get_spark_context()
        
        if self.metadata.has_band_dimension() != other.metadata.has_band_dimension():
            raise InternalException(message="one cube has band dimension, while the other doesn't: self=%s, other=%s"%(
                str(self.metadata.has_band_dimension()),
                str(other.metadata.has_band_dimension())
            ))

        if self._is_spatial() and other._is_spatial():
            raise FeatureUnsupportedException('Merging two cubes without time dimension is unsupported.')
        elif self._is_spatial():
            merged_data = self._apply_to_levels_geotrellis_rdd(
                lambda rdd, level:
                pysc._jvm.org.openeo.geotrellis.OpenEOProcesses().mergeCubes_SpaceTime_Spatial(
                    other.pyramid.levels[level].srdd.rdd(),
                    rdd,
                    overlaps_resolver,
                    True
                )
            )
        elif other._is_spatial():
            merged_data = self._apply_to_levels_geotrellis_rdd(
                lambda rdd, level:
                pysc._jvm.org.openeo.geotrellis.OpenEOProcesses().mergeCubes_SpaceTime_Spatial(
                    rdd,
                    other.pyramid.levels[level].srdd.rdd(),
                    overlaps_resolver,
                    False
                )
            )
        else:
            merged_data=self._apply_to_levels_geotrellis_rdd(
                lambda rdd, level:
                    pysc._jvm.org.openeo.geotrellis.OpenEOProcesses().mergeCubes(
                        rdd,
                        other.pyramid.levels[level].srdd.rdd(),
                        overlaps_resolver
                    )
            )

        if self.metadata.has_band_dimension() and other.metadata.has_band_dimension():
            for iband in other.metadata.bands:
                if iband.name not in merged_data.metadata.band_names:
                    merged_data.metadata=merged_data.metadata.append_band(iband)
        
        return merged_data

    def mask_polygon(self, mask: Union[Polygon, MultiPolygon], srs="EPSG:4326",
                     replacement=None, inside=False) -> 'GeotrellisTimeSeriesImageCollection':
        max_level = self.pyramid.levels[self.pyramid.max_zoom]
        layer_crs = max_level.layer_metadata.crs
        reprojected_polygon = self.__reproject_polygon(mask, "+init=" + srs, layer_crs)
        # TODO should we warn when masking generates an empty collection?
        # TODO: use `replacement` and `inside`
        return self.apply_to_levels(lambda rdd: rdd.mask(
            reprojected_polygon,
            partition_strategy=None,
            options=gps.RasterizerOptions()
        ))

    def mask(self, mask: 'GeotrellisTimeSeriesImageCollection',
             replacement=None) -> 'GeotrellisTimeSeriesImageCollection':
        # mask needs to be the same layout as this layer
        mask_pyramid_levels = {
            k: l.tile_to_layout(layout=self.pyramid.levels[k])
            for k, l in mask.pyramid.levels.items()
        }
        rasterMask = gps.get_spark_context()._jvm.org.openeo.geotrellis.OpenEOProcesses().rasterMask
        return self._apply_to_levels_geotrellis_rdd(
            lambda rdd, level: rasterMask(rdd, mask_pyramid_levels[level].srdd.rdd(), replacement)
        )

    def apply_kernel(self, kernel: np.ndarray, factor=1, border = 0, replace_invalid=0):

        pysc = gps.get_spark_context()

        #converting a numpy array into a geotrellis tile seems non-trivial :-)
        kernel = factor * kernel.astype(np.float64)
        kernel_tile = Tile.from_numpy_array(kernel, no_data_value=None)
        rdd = pysc.parallelize([(gps.SpatialKey(0,0), kernel_tile)])
        metadata = {'cellType': str(kernel.dtype),
                    'extent': {'xmin': 0.0, 'ymin': 0.0, 'xmax': 1.0, 'ymax': 1.0},
                    'crs': '+proj=longlat +datum=WGS84 +no_defs ',
                    'bounds': {
                        'minKey': {'col': 0, 'row': 0},
                        'maxKey': {'col': 0, 'row': 0}},
                    'layoutDefinition': {
                        'extent': {'xmin': 0.0, 'ymin': 0.0, 'xmax': 1.0, 'ymax': 1.0},
                        'tileLayout': {'tileCols': 5, 'tileRows': 5, 'layoutCols': 1, 'layoutRows': 1}}}
        geopyspark_layer = TiledRasterLayer.from_numpy_rdd(gps.LayerType.SPATIAL, rdd, metadata)
        geotrellis_tile = geopyspark_layer.srdd.rdd().collect()[0]._2().band(0)

        if self.pyramid.layer_type == gps.LayerType.SPACETIME:
            result_collection = self._apply_to_levels_geotrellis_rdd(
                lambda rdd, level: pysc._jvm.org.openeo.geotrellis.OpenEOProcesses().apply_kernel_spacetime(rdd, geotrellis_tile))
        else:
            result_collection = self._apply_to_levels_geotrellis_rdd(
                lambda rdd, level: pysc._jvm.org.openeo.geotrellis.OpenEOProcesses().apply_kernel_spatial(rdd,geotrellis_tile))
        return result_collection

    def apply_neighborhood(self, process:Dict, size:List,overlap:List) -> 'ImageCollection':

        spatial_dims = self.metadata.spatial_dimensions
        if len(spatial_dims) != 2:
            raise OpenEOApiException(message="Unexpected spatial dimensions in apply_neighborhood,"
                                             " expecting exactly 2 spatial dimensions: %s" % str(spatial_dims))
        x = spatial_dims[0]
        y = spatial_dims[1]
        size_dict = {e['dimension']:e for e in size}
        overlap_dict = {e['dimension']: e for e in overlap}
        if size_dict.get(x.name, {}).get('unit', None) != 'px' or size_dict.get(y.name, {}).get('unit', None) != 'px':
            raise OpenEOApiException(message="apply_neighborhood: window sizes for the spatial dimensions"
                                             " of this datacube should be specified in pixels."
                                             " This was provided: %s" % str(size))
        sizeX = int(size_dict[x.name]['value'])
        sizeY = int(size_dict[y.name]['value'])
        if sizeX < 32 or sizeY < 32:
            raise OpenEOApiException(message="apply_neighborhood: window sizes smaller then 32 are not yet supported.")
        overlap_x = overlap_dict.get(x.name,{'value': 0, 'unit': 'px'})
        overlap_y = overlap_dict.get(y.name,{'value': 0, 'unit': 'px'})
        if overlap_x.get('unit', None) != 'px' or overlap_y.get('unit', None) != 'px':
            raise OpenEOApiException(message="apply_neighborhood: overlap sizes for the spatial dimensions"
                                             " of this datacube should be specified, in pixels."
                                             " This was provided: %s" % str(overlap))
        jvm = self._get_jvm()
        overlap_x_value = int(overlap_x['value'])
        overlap_y_value = int(overlap_y['value'])
        retiled_collection = self._apply_to_levels_geotrellis_rdd(
            lambda rdd, level: jvm.org.openeo.geotrellis.OpenEOProcesses().retile(rdd, sizeX, sizeY, overlap_x_value, overlap_y_value))

        from openeogeotrellis.backend import SingleNodeUDFProcessGraphVisitor, GeoPySparkBackendImplementation

        process = GeoPySparkBackendImplementation.accept_process_graph(process)
        temporal_size = temporal_overlap = None
        if self.metadata.has_temporal_dimension():
            temporal_size = size_dict.get(self.metadata.temporal_dimension.name,None)
            temporal_overlap = overlap_dict.get(self.metadata.temporal_dimension.name, None)

        result_collection = None
        if isinstance(process, SingleNodeUDFProcessGraphVisitor):
            udf = process.udf_args.get('udf', None)
            context = process.udf_args.get('context', {})
            if not isinstance(udf, str):
                raise ValueError(
                    "The 'run_udf' process requires at least a 'udf' string argument, but got: '%s'." % udf)
            if temporal_size is None or temporal_size.get('value',None) is None:
                #full time dimension has to be provided
                result_collection = retiled_collection.apply_tiles_spatiotemporal(udf,context=context)
            elif temporal_size.get('value',None) == 'P1D' and temporal_overlap is None:
                result_collection = retiled_collection.apply_tiles(udf,context=context)
            else:
                raise OpenEOApiException(
                    message="apply_neighborhood: for temporal dimension,"
                            " either process all values, or only single date is supported for now!")

        elif isinstance(process, GeotrellisTileProcessGraphVisitor):
            if temporal_size is None or temporal_size.get('value', None) is None:
                raise OpenEOApiException(message="apply_neighborhood: only supporting complex callbacks on bands")
            elif temporal_size.get('value', None) == 'P1D' and temporal_overlap is None:
                result_collection = self.reduce_bands(process)
            else:
                raise OpenEOApiException(message="apply_neighborhood: only supporting complex callbacks on bands")
        else:
            raise OpenEOApiException(message="apply_neighborhood: only supporting callbacks with a single UDF.")

        if overlap_x_value > 0 or overlap_y_value > 0:

            result_collection = result_collection._apply_to_levels_geotrellis_rdd(
                lambda rdd, level: jvm.org.openeo.geotrellis.OpenEOProcesses().remove_overlap(rdd, sizeX, sizeY,
                                                                                      overlap_x_value,
                                                                                      overlap_y_value))

        return result_collection


    def resample_cube_spatial(self, target:'ImageCollection', method:str='near')-> 'ImageCollection':
        """
        Resamples the spatial dimensions (x,y) of this data cube to a target data cube and return the results as a new data cube.

        https://processes.openeo.org/#resample_cube_spatial

        :param target: An ImageCollection that specifies the target
        :param method: The resampling method.
        :return: A raster data cube with values warped onto the new projection.

        """
        resample_method = ResampleMethod(self._get_resample_method(method))
        if len(self.pyramid.levels)!=1 or len(target.pyramid.levels)!=1:
            raise FeatureUnsupportedException(message='This backend does not support resampling between full '
                                                      'pyramids, for instance used by viewing services. Batch jobs '
                                                      'should work.')
        max_level:TiledRasterLayer = self.pyramid.levels[self.pyramid.max_zoom]
        target_max_level:TiledRasterLayer = target.pyramid.levels[target.pyramid.max_zoom]
        level_rdd_tuple = self._get_jvm().org.openeo.geotrellis.OpenEOProcesses().resampleCubeSpatial(max_level.srdd.rdd(),target_max_level.srdd.rdd(),resample_method)

        layer = self._create_tilelayer(level_rdd_tuple._2(),max_level.layer_type,target.pyramid.max_zoom)
        pyramid = Pyramid({target.pyramid.max_zoom:layer})
        return GeotrellisTimeSeriesImageCollection(pyramid, self._service_registry, metadata=self.metadata)




    def resample_spatial(
            self,
            resolution: Union[float, Tuple[float, float]],
            projection: Union[int, str] = None,
            method: str = 'near',
            align: str = 'upper-left'
    ):
        """
        https://open-eo.github.io/openeo-api/v/0.4.0/processreference/#resample_spatial
        :param resolution:
        :param projection:
        :param method:
        :param align:
        :return:
        """

        # TODO: use align

        resample_method = self._get_resample_method(method)

        #IF projection is defined, we need to warp
        if projection is not None:
            reprojected = self.apply_to_levels(lambda layer: gps.TiledRasterLayer(
                layer.layer_type, layer.srdd.reproject(str(projection), resample_method, None)
            ))
            return reprojected
        elif resolution != 0.0:

            max_level = self.pyramid.levels[self.pyramid.max_zoom]
            extent = max_level.layer_metadata.layout_definition.extent

            if projection is not None:
                extent = self._reproject_extent(
                    max_level.layer_metadata.crs, projection, extent.xmin, extent.ymin, extent.xmax, extent.ymax
                )

            width = extent.xmax - extent.xmin
            height = extent.ymax - extent.ymin

            nbTilesX = width / (256 * resolution)
            nbTilesY = height / (256 * resolution)

            exactTileSizeX = width/(resolution * math.ceil(nbTilesX))
            exactNbTilesX = width/(resolution * exactTileSizeX)

            exactTileSizeY = height / (resolution * math.ceil(nbTilesY))
            exactNbTilesY = height / (resolution * exactTileSizeY)


            newLayout = gps.LayoutDefinition(extent=extent,tileLayout=gps.TileLayout(int(exactNbTilesX),int(exactNbTilesY),int(exactTileSizeX),int(exactTileSizeY)))

            if(projection is not None):
                resampled = max_level.tile_to_layout(newLayout,target_crs=projection, resample_method=resample_method)
            else:
                resampled = max_level.tile_to_layout(newLayout,resample_method=resample_method)

            pyramid = Pyramid({0:resampled})
            return GeotrellisTimeSeriesImageCollection(pyramid, self._service_registry,
                                                       metadata=self.metadata)
            #return self.apply_to_levels(lambda layer: layer.tile_to_layout(projection, resample_method))
        return self

    def _get_resample_method(self, method):
        resample_method = {
            'bilinear': gps.ResampleMethod.BILINEAR,
            'average': gps.ResampleMethod.AVERAGE,
            'cubic': gps.ResampleMethod.CUBIC_CONVOLUTION,
            'cubicspline': gps.ResampleMethod.CUBIC_SPLINE,
            'lanczos': gps.ResampleMethod.LANCZOS,
            'mode': gps.ResampleMethod.MODE,
            'max': gps.ResampleMethod.MAX,
            'min': gps.ResampleMethod.MIN,
            'med': gps.ResampleMethod.MEDIAN,
        }.get(method, gps.ResampleMethod.NEAREST_NEIGHBOR)
        return resample_method

    def linear_scale_range(self, input_min, input_max, output_min, output_max) -> 'ImageCollection':
        """ Color stretching
            :param input_min: Minimum input value
            :param input_max: Maximum input value
            :param output_min: Minimum output value
            :param output_max: Maximum output value
            :return An ImageCollection instance
        """
        rescaled = self.apply_to_levels(lambda layer: layer.normalize(output_min, output_max, input_min, input_max))
        output_range = output_max - output_min
        if output_range >1 and type(output_min) == int and type(output_max) == int:
            if output_range < 254 and output_min >= 0:
                rescaled = rescaled.apply_to_levels(lambda layer: layer.convert_data_type(gps.CellType.UINT8,255))
            elif output_range < 65535 and output_min >= 0:
                rescaled = rescaled.apply_to_levels(lambda layer: layer.convert_data_type(gps.CellType.UINT16))
        return rescaled

    def timeseries(self, x, y, srs="EPSG:4326") -> Dict:
        max_level = self.pyramid.levels[self.pyramid.max_zoom]
        (x_layer,y_layer) = pyproj.transform(pyproj.Proj(init=srs),pyproj.Proj(max_level.layer_metadata.crs),x,y)
        points = [
            Point(x_layer, y_layer),
        ]
        values = max_level.get_point_values(points)
        result = {}
        if isinstance(values[0][1],List):
            values = values[0][1]
        for v in values:
            if isinstance(v,float):
                result["NoDate"]=v
            elif "isoformat" in dir(v[0]):
                result[v[0].isoformat()]=v[1]
            elif v[0] is None:
                #empty timeseries
                pass
            else:
                print("unexpected value: "+str(v))

        return result

    def raster_to_vector(self):
        """
        Outputs polygons, where polygons are formed from homogeneous zones of four-connected neighbors
        @return:
        """
        max_level = self.pyramid.levels[self.pyramid.max_zoom]
        with tempfile.NamedTemporaryFile(suffix=".json.tmp",delete=False) as temp_file:
            gps.get_spark_context()._jvm.org.openeo.geotrellis.OpenEOProcesses().vectorize(max_level.srdd.rdd(),temp_file.name)
            #postpone turning into an actual collection upon usage
            return DelayedVector(temp_file.name)


    def zonal_statistics(self, regions: Union[str, GeometryCollection, Polygon, MultiPolygon], func) -> AggregatePolygonResult:
        # TODO: rename to aggregate_polygon?
        # TODO eliminate code duplication
        _log.info("zonal_statistics with {f!r}, {r}".format(f=func, r=type(regions)))

        def insert_timezone(instant):
            return instant.replace(tzinfo=pytz.UTC) if instant.tzinfo is None else instant

        from_vector_file = isinstance(regions, str)
        multiple_geometries = from_vector_file or isinstance(regions, GeometryCollection)

        if func in ['histogram', 'sd', 'median']:
            highest_level = self.pyramid.levels[self.pyramid.max_zoom]
            layer_metadata = highest_level.layer_metadata
            scala_data_cube = highest_level.srdd.rdd()
            polygons = to_projected_polygons(self._get_jvm(), regions)
            from_date = insert_timezone(layer_metadata.bounds.minKey.instant)
            to_date = insert_timezone(layer_metadata.bounds.maxKey.instant)

            # TODO also add dumping results first to temp json file like with "mean"
            if func == 'histogram':
                stats = self._compute_stats_geotrellis().compute_histograms_time_series_from_datacube(
                    scala_data_cube, polygons, from_date.isoformat(), to_date.isoformat(), 0
                )
            elif func == 'sd':
                stats = self._compute_stats_geotrellis().compute_sd_time_series_from_datacube(
                    scala_data_cube, polygons, from_date.isoformat(), to_date.isoformat(), 0
                )
            elif func == 'median':
                stats = self._compute_stats_geotrellis().compute_median_time_series_from_datacube(
                    scala_data_cube, polygons, from_date.isoformat(), to_date.isoformat(), 0
                )
            else:
                raise ValueError(func)

            return AggregatePolygonResult(
                timeseries=self._as_python(stats),
                # TODO: regions can also be a string (path to vector file) instead of geometry object
                regions=regions if multiple_geometries else GeometryCollection([regions]),
            )
        elif func == "mean":
            if multiple_geometries:
                highest_level = self.pyramid.levels[self.pyramid.max_zoom]
                layer_metadata = highest_level.layer_metadata
                scala_data_cube = highest_level.srdd.rdd()
                polygons = to_projected_polygons(self._get_jvm(), regions)
                from_date = insert_timezone(layer_metadata.bounds.minKey.instant)
                to_date = insert_timezone(layer_metadata.bounds.maxKey.instant)

                with tempfile.NamedTemporaryFile(suffix=".json.tmp") as temp_file:
                    self._compute_stats_geotrellis().compute_average_timeseries_from_datacube(
                        scala_data_cube,
                        polygons,
                        from_date.isoformat(),
                        to_date.isoformat(),
                        0,
                        temp_file.name
                    )
                    with open(temp_file.name, encoding='utf-8') as f:
                        timeseries = json.load(f)
                return AggregatePolygonResult(
                    timeseries=timeseries,
                    # TODO: regions can also be a string (path to vector file) instead of geometry object
                    regions=regions,
                )
            else:
                return AggregatePolygonResult(
                    timeseries=self.polygonal_mean_timeseries(regions),
                    regions=GeometryCollection([regions]),
                )
        else:
            raise ValueError(func)

    def _compute_stats_geotrellis(self):
        accumulo_instance_name = 'hdp-accumulo-instance'
        return self._get_jvm().org.openeo.geotrellis.ComputeStatsGeotrellisAdapter(self._zookeepers(), accumulo_instance_name)

    def _zookeepers(self):
        return ','.join(ConfigParams().zookeepernodes)

    # FIXME: define this somewhere else?
    def _as_python(self, java_object):
        """
        Converts Java collection objects retrieved from Py4J to their Python counterparts, recursively.
        :param java_object: a JavaList or JavaMap
        :return: a Python list or dictionary, respectively
        """

        from py4j.java_collections import JavaList, JavaMap

        if isinstance(java_object, JavaList):
            return [self._as_python(elem) for elem in list(java_object)]

        if isinstance(java_object, JavaMap):
            return {self._as_python(key): self._as_python(value) for key, value in dict(java_object).items()}

        return java_object

    def polygonal_mean_timeseries(self, polygon: Union[Polygon, MultiPolygon]) -> Dict:
        max_level = self.pyramid.levels[self.pyramid.max_zoom]
        layer_crs = max_level.layer_metadata.crs
        reprojected_polygon = GeotrellisTimeSeriesImageCollection.__reproject_polygon(polygon, "+init=EPSG:4326" ,layer_crs)

        #TODO somehow mask function was masking everything, while the approach with direct timeseries computation did not have issues...
        masked_layer = max_level.mask(reprojected_polygon)

        no_data = masked_layer.layer_metadata.no_data_value

        def combine_cells(acc: List[Tuple[int, int]], tile) -> List[Tuple[int, int]]:  # [(sum, count)]
            n_bands = len(tile.cells)

            if not acc:
                acc = [(0, 0)] * n_bands

            for i in range(n_bands):
                grid = tile.cells[i]

                # special treatment for a UDF layer (NO_DATA is nan so every value, including nan, is not equal to nan)
                without_no_data = (~np.isnan(grid)) & (grid != no_data)

                sum = grid[without_no_data].sum()
                count = without_no_data.sum()

                acc[i] = acc[i][0] + sum, acc[i][1] + count

            return acc

        def combine_values(l1: List[Tuple[int, int]], l2: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
            for i in range(len(l2)):
                l1[i] = l1[i][0] + l2[i][0], l1[i][1] + l2[i][1]

            return l1

        polygon_mean_by_timestamp = masked_layer.to_numpy_rdd() \
            .map(lambda pair: (pair[0].instant, pair[1])) \
            .aggregateByKey([], combine_cells, combine_values)

        def to_mean(values: Tuple[int, int]) -> float:
            sum, count = values
            return sum / count

        collected = polygon_mean_by_timestamp.collect()
        return {timestamp.isoformat(): [[to_mean(v) for v in values]] for timestamp, values in collected}

    def _to_xarray(self):
        spatial_rdd = self.pyramid.levels[self.pyramid.max_zoom]
        return self._collect_as_xarray(spatial_rdd)

    def download(self,outputfile:str, **format_options) -> str:
        """
        Extracts into various formats from this image collection.
        
        Supported formats:
        * GeoTIFF: raster with the limitation that it only export bands at a single (random) date 
        * NetCDF: raster, currently using h5NetCDF
        * JSON: the json serialization of the underlying xarray, with extra attributes such as value/coord dtypes, crs, nodata value
        """
        #geotiffs = self.rdd.merge().to_geotiff_rdd(compression=gps.Compression.DEFLATE_COMPRESSION).collect()
        format=format_options.get("format", "GTiff").upper()
        
        filename = outputfile
        if outputfile is None:
            _, filename = tempfile.mkstemp(suffix='.oeo-gps-dl')
        else:
            filename = outputfile

        # get the data at highest resolution
        spatial_rdd = self.pyramid.levels[self.pyramid.max_zoom]

        # spatial bounds        
        xmin, ymin, xmax, ymax = format_options.get('left'), format_options.get('bottom'),\
                                 format_options.get('right'), format_options.get('top')

        if xmin and ymin and xmax and ymax:
            srs = format_options.get('srs', 'EPSG:4326')
            if srs is None:
                srs = 'EPSG:4326'

            src_crs = "+init=" + srs
            dst_crs = spatial_rdd.layer_metadata.crs
            crop_bounds = self._reproject_extent(src_crs, dst_crs, xmin, ymin, xmax, ymax)
        else:
            crop_bounds = None

        # date bounds        
        datefrom,dateto= format_options.get('from'), format_options.get('to')
        if datefrom and dateto:
            crop_dates=(pd.Timestamp(datefrom), pd.Timestamp(dateto))
        else:
            crop_dates=None

        tiled = format_options.get("tiled", False)          
        catalog = format_options.get("parameters", {}).get("catalog", False)

        if format == "GTIFF":
            if spatial_rdd.layer_type != gps.LayerType.SPATIAL:
                spatial_rdd = spatial_rdd.to_spatial_layer()

            zlevel = format_options.get("ZLEVEL",6)
            if catalog:
                self._save_on_executors(spatial_rdd, filename)
            elif tiled:
                band_count = 1
                if self.metadata.has_band_dimension():
                    band_count = len(self.metadata.band_dimension.band_names)
                self._get_jvm().org.openeo.geotrellis.geotiff.package.saveRDD(spatial_rdd.srdd.rdd(),band_count,filename,zlevel)
            else:
                self._save_stitched(spatial_rdd, filename, crop_bounds,zlevel=zlevel)

        elif format == "NETCDF":
            if not tiled:
                result=self._collect_as_xarray(spatial_rdd, crop_bounds, crop_dates)
            else:
                result=self._collect_as_xarray(spatial_rdd)
            # rearrange in a basic way because older xarray versions have a bug and ellipsis don't work in xarray.transpose()
            l=list(result.dims[:-2])
            result=result.transpose(*(l+['y','x']))
            # turn it into a dataset where each band becomes a variable
            if not 'bands' in result.dims:
                result=result.expand_dims(dim={'bands':['band_0']})
            result=result.to_dataset('bands')
            #result=result.assign_coords(y=result.y[::-1])
            # TODO: NETCDF4 is broken. look into
            result.to_netcdf(filename, engine='h5netcdf') # engine='scipy')

        elif format == "JSON":
            # saving to json, this is potentially big in memory
            # get result as xarray
            if not tiled:
                result=self._collect_as_xarray(spatial_rdd, crop_bounds, crop_dates)
            else:
                result=self._collect_as_xarray(spatial_rdd)
            jsonresult=result.to_dict()
            # add attributes that needed for re-creating xarray from json
            jsonresult['attrs']['dtype']=str(result.values.dtype)
            jsonresult['attrs']['shape']=list(result.values.shape)
            for i in result.coords.values():
                jsonresult['coords'][i.name]['attrs']['dtype']=str(i.dtype)
                jsonresult['coords'][i.name]['attrs']['shape']=list(i.shape)
            result=None
            # custom print so resulting json is easy to read humanly
            with open(filename,'w') as f:
                def custom_print(data_structure, indent=1):
                    f.write("{\n")
                    needs_comma=False
                    for key, value in data_structure.items():
                        if needs_comma: 
                            f.write(',\n')
                        needs_comma=True
                        f.write('  '*indent+json.dumps(key)+':')
                        if isinstance(value, dict): 
                            custom_print(value, indent+1)
                        else: 
                            json.dump(value,f,default=str,separators=(',',':'))
                    f.write('\n'+'  '*(indent-1)+"}")
                    
                custom_print(jsonresult)

        else:
            raise OpenEOApiException(
                message="Format {f!r} is not supported".format(f=format),
                code="FormatUnsupported", status_code=400
            )
        return filename

    def _collect_as_xarray(self, rdd, crop_bounds=None, crop_dates=None):
            
        # windows/dims are tuples of (xmin/mincol,ymin/minrow,width/cols,height/rows)
        layout_pix=rdd.layer_metadata.layout_definition.tileLayout
        layout_win=(0, 0, layout_pix.layoutCols*layout_pix.tileCols, layout_pix.layoutRows*layout_pix.tileRows)
        layout_extent=rdd.layer_metadata.layout_definition.extent
        layout_dim=(layout_extent.xmin, layout_extent.ymin, layout_extent.xmax-layout_extent.xmin, layout_extent.ymax-layout_extent.ymin)
        xres=layout_dim[2]/layout_win[2]
        yres=layout_dim[3]/layout_win[3]
        if crop_bounds:
            xmin=math.floor((crop_bounds.xmin-layout_extent.xmin)/xres)
            ymin=math.floor((crop_bounds.ymin-layout_extent.ymin)/yres)
            xmax= math.ceil((crop_bounds.xmax-layout_extent.xmin)/xres)
            ymax= math.ceil((crop_bounds.ymax-layout_extent.ymin)/yres)
            crop_win=(xmin, ymin, xmax-xmin, ymax-ymin)
        else:
            xmin=rdd.layer_metadata.bounds.minKey.col
            xmax=rdd.layer_metadata.bounds.maxKey.col+1
            ymin=rdd.layer_metadata.bounds.minKey.row
            ymax=rdd.layer_metadata.bounds.maxKey.row+1
            crop_win=(xmin*layout_pix.tileCols, ymin*layout_pix.tileRows, (xmax-xmin)*layout_pix.tileCols, (ymax-ymin)*layout_pix.tileRows)
        crop_dim=(layout_dim[0]+crop_win[0]*xres, layout_dim[1]+crop_win[1]*yres, crop_win[2]*xres, crop_win[3]*yres)
            

        # build metadata for the xarrays
        # coordinates are in the order of t,bands,x,y
        dims=[]
        coords={}
        has_time=self.metadata.has_temporal_dimension()
        if has_time:
            dims.append('t')
        has_bands=self.metadata.has_band_dimension()
        if has_bands:
            dims.append('bands')
            coords['bands']=self.metadata.band_names
        dims.append('x')
        coords['x']=np.linspace(crop_dim[0]+0.5*xres, crop_dim[0]+crop_dim[2]-0.5*xres, crop_win[2])
        dims.append('y')
        coords['y']=np.linspace(crop_dim[1]+0.5*yres, crop_dim[1]+crop_dim[3]-0.5*yres, crop_win[3])
        
        def stitch_at_time(crop_win, layout_win, items):
            
            # value expected to be another tuple with the original spacetime key and the array
            subarrs=list(items[1])
                        
            # get block sizes
            bw,bh=subarrs[0][1].cells.shape[-2:]
            bbands=sum(subarrs[0][1].cells.shape[:-2]) if len(subarrs[0][1].cells.shape)>2 else 1
            wbind=np.arange(0,bbands)
            dtype=subarrs[0][1].cells.dtype
            nodata=subarrs[0][1].no_data_value
            
            # allocate collector ndarray
            if nodata:
                window=np.full((bbands,crop_win[2],crop_win[3]), nodata, dtype)
            else:
                window=np.empty((bbands,crop_win[2],crop_win[3]), dtype)
            wxind=np.arange(crop_win[0],crop_win[0]+crop_win[2])
            wyind=np.arange(crop_win[1],crop_win[1]+crop_win[3])

            # override classic bottom-left corner coord system to top-left
            # note that possible key types are SpatialKey and SpaceTimeKey, but since at this level only col/row is used, casting down to SpatialKey
            switch_topleft=True
            tp=(0,1,2)
            if switch_topleft:
                nyblk=int(layout_win[3]/bh)-1
                subarrs=list(map(
                    lambda t: ( SpatialKey(t[0].col,nyblk-t[0].row), t[1] ),
                    subarrs
                ))
                tp=(0,2,1)
            
            # loop over blocks and merge into
            for iblk in subarrs:
                iwin=(iblk[0].col*bw, iblk[0].row*bh, bw, bh)
                iarr=iblk[1].cells
                iarr=iarr.reshape((-1,bh,bw)).transpose(tp)
                ixind=np.arange(iwin[0],iwin[0]+iwin[2])
                iyind=np.arange(iwin[1],iwin[1]+iwin[3])
                if switch_topleft:
                    iyind=iyind[::-1]
                xoverlap= np.intersect1d(wxind,ixind,True,True)
                yoverlap= np.intersect1d(wyind,iyind,True,True)
                if len(xoverlap[1])>0 and len(yoverlap[1]>0):
                    window[np.ix_(wbind,xoverlap[1],yoverlap[1])]=iarr[np.ix_(wbind,xoverlap[2],yoverlap[2])]
                    
            # return date (or None) - window tuple
            return (items[0],window)

        # at every date stitch together the layer, still on the workers   
        #mapped=list(map(lambda t: (t[0].row,t[0].col),rdd.to_numpy_rdd().collect())); min(mapped); max(mapped)
        from functools import partial
        collection=rdd\
            .to_numpy_rdd()\
            .filter(lambda t: (t[0].instant>=crop_dates[0] and t[0].instant<=crop_dates[1]) if has_time and crop_dates != None else True)\
            .map(lambda t: (t[0].instant if has_time else None, (t[0], t[1])))\
            .groupByKey()\
            .map(partial(stitch_at_time, crop_win, layout_win))\
            .collect()
#         collection=rdd\
#             .to_numpy_rdd()\
#             .filter(lambda t: (t[0].instant>=crop_dates[0] and t[0].instant<=crop_dates[1]) if has_time else True)\
#             .map(lambda t: (t[0].instant if has_time else None, (t[0], t[1])))\
#             .groupByKey()\
#             .collect()
#         collection=list(map(partial(stitch_at_time, crop_win, layout_win),collection))
        
        
        if len(collection)==0:
            return xr.DataArray(np.full([0]*len(dims),0),dims=dims,coords=dict(map(lambda k: (k[0],[]),coords.items())))
        
        if len(collection)>1:
            collection.sort(key= lambda i: i[0])
                        
        if not has_bands:
            collection=list(map(lambda i: (i[0],i[1].reshape(list(i[1].shape[-2:]))), collection))

        # collect to an xarray
        if has_time:
            collection=list(zip(*collection))
            coords['t']=list(map(lambda i: np.datetime64(i),collection[0]))
            npresult=np.stack(collection[1])
            # TODO: this is a workaround if metadata goes out of sync, fix upstream process nodes to update metdata
            if len(coords['bands'])!=npresult.shape[-3]:
                coords['bands']=[ 'band_'+str(i) for i in range(npresult.shape[-3])]
            result=xr.DataArray(npresult,dims=dims,coords=coords)
        else:
            # TODO error if len > 1
            result=xr.DataArray(collection[0][1],dims=dims,coords=coords)
            
        # add some metadata
        result=result.assign_attrs(dict(
            # TODO: layer_metadata is always 255, regardless of dtype, only correct inside the rdd-s
            nodata=rdd.layer_metadata.no_data_value,
            # TODO: crs seems to be recognized when saving to netcdf and loading with gdalinfo/qgis, but yet projection is incorrect https://github.com/pydata/xarray/issues/2288
            crs=rdd.layer_metadata.crs
        ))
        
        return result

        

    def _reproject_extent(self, src_crs, dst_crs, xmin, ymin, xmax, ymax):
        src_proj = pyproj.Proj(src_crs)
        dst_proj = pyproj.Proj(dst_crs)

        def reproject_point(x, y):
            return pyproj.transform(
                src_proj,
                dst_proj,
                x, y
            )

        reprojected_xmin, reprojected_ymin = reproject_point(xmin, ymin)
        reprojected_xmax, reprojected_ymax = reproject_point(xmax, ymax)
        crop_bounds = \
            Extent(xmin=reprojected_xmin, ymin=reprojected_ymin, xmax=reprojected_xmax, ymax=reprojected_ymax)
        return crop_bounds

    def _save_on_executors(self, spatial_rdd: gps.TiledRasterLayer, path,zlevel=6):
        geotiff_rdd = spatial_rdd.to_geotiff_rdd(
            storage_method=gps.StorageMethod.TILED,
            compression=gps.Compression.DEFLATE_COMPRESSION
        )

        basedir = pathlib.Path(str(path) + '.catalogresult')
        basedir.mkdir(parents=True, exist_ok=True)

        def write_tiff(item):
            key, data = item
            path = basedir / '{c}-{r}.tiff'.format(c=key.col, r=key.row)
            with path.open('wb') as f:
                f.write(data)

        geotiff_rdd.foreach(write_tiff)
        tiffs = [str(path.absolute()) for path in basedir.glob('*.tiff')]

        _log.info("Merging results {t!r}".format(t=tiffs))
        merge_args = [ "-o", path, "-of", "GTiff", "-co", "COMPRESS=DEFLATE", "-co", "TILED=TRUE","-co","ZLEVEL=%s"%zlevel]
        merge_args += tiffs
        _log.info("Executing: {a!r}".format(a=merge_args))
        #xargs avoids issues with too many args
        subprocess.run(['xargs', '-0', 'gdal_merge.py'], input='\0'.join(merge_args), universal_newlines=True)


    def _save_stitched(self, spatial_rdd, path, crop_bounds=None,zlevel=6):
        jvm = self._get_jvm()

        max_compression = jvm.geotrellis.raster.io.geotiff.compression.DeflateCompression(zlevel)

        if crop_bounds:
            jvm.org.openeo.geotrellis.geotiff.package.saveStitched(spatial_rdd.srdd.rdd(), path, crop_bounds._asdict(),
                                                                   max_compression)
        else:
            jvm.org.openeo.geotrellis.geotiff.package.saveStitched(spatial_rdd.srdd.rdd(), path, max_compression)

    def _save_stitched_tiled(self, spatial_rdd, filename):
        import rasterio as rstr
        from affine import Affine
        import rasterio._warp as rwarp

        max_level = self.pyramid.levels[self.pyramid.max_zoom]

        spatial_rdd = spatial_rdd.persist()

        sorted_keys = sorted(spatial_rdd.collect_keys())

        upper_left_coords = GeotrellisTimeSeriesImageCollection._mapTransform(max_level.layer_metadata.layout_definition, sorted_keys[0])
        lower_right_coords = GeotrellisTimeSeriesImageCollection._mapTransform(max_level.layer_metadata.layout_definition, sorted_keys[-1])

        data = spatial_rdd.stitch()

        bands, w, h = data.cells.shape
        nodata = max_level.layer_metadata.no_data_value
        dtype = data.cells.dtype
        ex = Extent(xmin=upper_left_coords.left, ymin=lower_right_coords.bottom, xmax=lower_right_coords.right, ymax=upper_left_coords.top)
        cw, ch = (ex.xmax - ex.xmin) / w, (ex.ymax - ex.ymin) / h
        overview_level = int(math.log(w) / math.log(2) - 8)

        with rstr.io.MemoryFile() as memfile, open(filename, 'wb') as f:
            with memfile.open(driver='GTiff',
                              count=bands,
                              width=w,
                              height=h,
                              transform=Affine(cw, 0.0, ex.xmin,
                                               0.0, -ch, ex.ymax),
                              crs=rstr.crs.CRS.from_proj4(spatial_rdd.layer_metadata.crs),
                              nodata=nodata,
                              dtype=dtype,
                              compress='lzw',
                              tiled=True) as mem:
                windows = list(mem.block_windows(1))
                for _, w in windows:
                    segment = data.cells[:, w.row_off:(w.row_off + w.height), w.col_off:(w.col_off + w.width)]
                    mem.write(segment, window=w)
                    mask_value = np.all(segment != nodata, axis=0).astype(np.uint8) * 255
                    mem.write_mask(mask_value, window=w)

                    overviews = [2 ** j for j in range(1, overview_level + 1)]
                    mem.build_overviews(overviews, rwarp.Resampling.nearest)
                    mem.update_tags(ns='rio_oveview', resampling=rwarp.Resampling.nearest.value)

            while True:
                chunk = memfile.read(8192)
                if not chunk:
                    break

                f.write(chunk)

    def _proxy_tms(self,tms):
        if ConfigParams().is_ci_context:
            return tms.url_pattern
        else:
            host = tms.host
            port = tms.port
            self._proxy(host, port)
            url = "http://openeo.vgt.vito.be/tile/{z}/{x}/{y}.png"
            return url

    def _proxy(self, host, port):
        from kazoo.client import KazooClient
        zk = KazooClient(hosts=self._zookeepers())
        zk.start()
        try:
            zk.ensure_path("discovery/services/openeo-viewer-test")
            # id = uuid.uuid4()
            # print(id)
            id = 0
            zk.ensure_path("discovery/services/openeo-viewer-test/" + str(id))
            zk.set("discovery/services/openeo-viewer-test/" + str(id), str.encode(json.dumps(
                {"name": "openeo-viewer-test", "id": str(id), "address": host, "port": port, "sslPort": None,
                 "payload": None, "registrationTimeUTC": datetime.utcnow().strftime('%s'), "serviceType": "DYNAMIC"})))
        finally:
            zk.stop()
            zk.close()

    def ndvi(self, **kwargs) -> 'GeotrellisTimeSeriesImageCollection':
        return self._ndvi_v10(**kwargs) if 'target_band' in kwargs else self._ndvi_v04(**kwargs)

    def _ndvi_v04(self, name: str = None) -> 'GeotrellisTimeSeriesImageCollection':
        """0.4-style of ndvi process"""
        try:
            red_index, = [i for i, b in enumerate(self.metadata.bands) if b.common_name == 'red']
            nir_index, = [i for i, b in enumerate(self.metadata.bands) if b.common_name == 'nir']
        except ValueError:
            raise ValueError("Failed to detect 'red' and 'nir' bands")

        ndvi_collection = self._ndvi_collection(red_index, nir_index)

        # a single band that defaults to 'ndvi'
        ndvi_metadata = self.metadata \
            .reduce_dimension("bands") \
            .add_dimension(type="bands", name="bands", label=name or 'ndvi')

        return GeotrellisTimeSeriesImageCollection(
            ndvi_collection.pyramid,
            self._service_registry,
            ndvi_metadata
        )

    def _ndvi_v10(self, nir: str = None, red: str = None, target_band: str = None) -> 'GeotrellisTimeSeriesImageCollection':
        """1.0-style of ndvi process"""
        if not self.metadata.has_band_dimension():
            raise OpenEOApiException(
                status_code=400,
                code="DimensionAmbiguous",
                message="dimension of type `bands` is not available or is ambiguous.",
            )

        if target_band and target_band in self.metadata.band_names:
            raise OpenEOApiException(
                status_code=400,
                code="BandExists",
                message="A band with the specified target name exists.",
            )

        def first_index_if(coll, pred, *fallbacks):
            try:
                return next((i for i, elem in enumerate(coll) if pred(elem)))
            except StopIteration:
                if fallbacks:
                    head, *tail = fallbacks
                    return first_index_if(coll, head, *tail)
                else:
                    return None

        if not red:
            red_index = first_index_if(self.metadata.bands, lambda b: b.common_name == 'red')
        else:
            red_index = first_index_if(self.metadata.bands, lambda b: b.name == red, lambda b: b.common_name == red)

        if not nir:
            nir_index = first_index_if(self.metadata.bands, lambda b: b.common_name == 'nir')
        else:
            nir_index = first_index_if(self.metadata.bands, lambda b: b.name == nir, lambda b: b.common_name == nir)

        if red_index is None:
            raise OpenEOApiException(
                status_code=400,
                code="RedBandAmbiguous",
                message="The red band can't be resolved, please specify a band name.",
            )
        if nir_index is None:
            raise OpenEOApiException(
                status_code=400,
                code="NirBandAmbiguous",
                message="The NIR band can't be resolved, please specify a band name.",
            )

        ndvi_collection = self._ndvi_collection(red_index, nir_index)

        if target_band:  # append a new band named $target_band
            result_collection = self \
                .apply_to_levels(lambda layer: layer.convert_data_type("float32")) \
                .merge(ndvi_collection)

            result_metadata = self.metadata.append_band(Band(name=target_band, common_name=target_band, wavelength_um=None))
        else:  # drop all bands
            result_collection = ndvi_collection
            result_metadata = self.metadata.reduce_dimension("bands")

        return GeotrellisTimeSeriesImageCollection(
            result_collection.pyramid,
            self._service_registry,
            result_metadata
        )

    def _ndvi_collection(self, red_index: int, nir_index: int) -> 'GeotrellisTimeSeriesImageCollection':
        reduce_graph = {
            "red": {
                "process_id": "array_element",
                "arguments": {"data": {"from_parameter": "data"}, "index": red_index}
            },
            "nir": {
                "process_id": "array_element",
                "arguments": {"data": {"from_parameter": "data"}, "index": nir_index}
            },
            "nirminusred": {
                "process_id": "subtract",
                "arguments": {
                    "x": {"from_node": "nir"},
                    "y": {"from_node": "red"},
                }
            },
            "nirplusred": {
                "process_id": "add",
                "arguments": {
                    "x": {"from_node": "nir"},
                    "y": {"from_node": "red"},
                }
            },
            "ndvi": {
                "process_id": "divide",
                "arguments": {
                    "x": {"from_node": "nirminusred"},
                    "y": {"from_node": "nirplusred"},
                },
                "result": True,
            },
        }

        from openeogeotrellis.geotrellis_tile_processgraph_visitor import GeotrellisTileProcessGraphVisitor
        visitor = GeotrellisTileProcessGraphVisitor()

        return self.reduce_bands(visitor.accept_process_graph(reduce_graph))
