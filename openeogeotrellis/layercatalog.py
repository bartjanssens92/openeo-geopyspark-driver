import logging

from geopyspark import TiledRasterLayer, LayerType
from py4j.java_gateway import JavaGateway

from openeo import ImageCollection, List
from openeo.imagecollection import CollectionMetadata
from openeo_driver.backend import CollectionCatalog
from openeo_driver.utils import read_json
from openeogeotrellis.GeotrellisImageCollection import GeotrellisTimeSeriesImageCollection
from openeogeotrellis.configparams import ConfigParams
from openeogeotrellis.service_registry import InMemoryServiceRegistry
from openeogeotrellis.utils import kerberos, dict_merge_recursive, normalize_date

logger = logging.getLogger(__name__)


class GeoPySparkLayerCatalog(CollectionCatalog):

    # TODO: eliminate the dependency/coupling with service registry

    def __init__(self, all_metadata: List[dict], service_registry: InMemoryServiceRegistry):
        super().__init__(all_metadata=all_metadata)
        self._service_registry = service_registry

    def _strip_private_metadata(self, d: dict) -> dict:
        """Strip fields starting with underscore from a dictionary."""
        return {k: v for (k, v) in d.items() if not k.startswith('_')}

    def get_all_metadata(self) -> List[dict]:
        return [self._strip_private_metadata(d) for d in super().get_all_metadata()]

    def get_collection_metadata(self, collection_id, strip_private=True) -> dict:
        metadata = super().get_collection_metadata(collection_id)
        if strip_private:
            metadata = self._strip_private_metadata(metadata)
        return metadata

    def load_collection(self, collection_id: str, viewing_parameters: dict) -> ImageCollection:
        logger.info("Creating layer for {c} with viewingParameters {v}".format(c=collection_id, v=viewing_parameters))

        # TODO is it necessary to do this kerberos stuff here?
        kerberos()

        layer_metadata = self.get_collection_metadata(collection_id, strip_private=False)
        layer_source_info = layer_metadata.get("_vito", {}).get("data_source", {})
        layer_source_type = layer_source_info.get("type", "Accumulo").lower()

        import geopyspark as gps
        from_date = normalize_date(viewing_parameters.get("from", None))
        to_date = normalize_date(viewing_parameters.get("to", None))

        left = viewing_parameters.get("left", None)
        right = viewing_parameters.get("right", None)
        top = viewing_parameters.get("top", None)
        bottom = viewing_parameters.get("bottom", None)
        srs = viewing_parameters.get("srs", None)
        band_indices = viewing_parameters.get("bands")
        pysc = gps.get_spark_context()
        extent = None

        gateway = JavaGateway(eager_load=True, gateway_parameters=pysc._gateway.gateway_parameters)
        jvm = gateway.jvm
        if (left is not None and right is not None and top is not None and bottom is not None):
            extent = jvm.geotrellis.vector.Extent(float(left), float(bottom), float(right), float(top))

        def accumulo_pyramid():
            pyramidFactory = jvm.org.openeo.geotrellisaccumulo.PyramidFactory("hdp-accumulo-instance",
                                                                              ','.join(ConfigParams().zookeepernodes))
            accumulo_layer_name = layer_source_info['data_id']
            return pyramidFactory.pyramid_seq(accumulo_layer_name, extent, srs, from_date, to_date)

        def s3_pyramid():
            endpoint = layer_source_info['endpoint']
            region = layer_source_info['region']
            bucket_name = layer_source_info['bucket_name']

            return jvm.org.openeo.geotrelliss3.PyramidFactory(endpoint, region, bucket_name) \
                .pyramid_seq(extent, srs, from_date, to_date)

        def s3_jp2_pyramid():
            endpoint = layer_source_info['endpoint']
            region = layer_source_info['region']

            return jvm.org.openeo.geotrelliss3.Jp2PyramidFactory(endpoint, region) \
                .pyramid_seq(extent, srs, from_date, to_date, band_indices)

        def file_pyramid():
            return jvm.org.openeo.geotrellis.file.Sentinel2RadiometryPyramidFactory() \
                .pyramid_seq(extent, srs, from_date, to_date, band_indices)

        def sentinel_hub_pyramid():
            return jvm.org.openeo.geotrellis.file.Sentinel1Gamma0PyramidFactory() \
                .pyramid_seq(layer_source_info.get('uuid'),extent, srs, from_date, to_date, band_indices)

        if layer_source_type == 's3':
            pyramid = s3_pyramid()
        elif layer_source_type == 's3-jp2':
            pyramid = s3_jp2_pyramid()
        elif layer_source_type == 'file':
            pyramid = file_pyramid()
        elif layer_source_type == 'sentinel-hub':
            pyramid = sentinel_hub_pyramid()
        else:
            pyramid = accumulo_pyramid()

        temporal_tiled_raster_layer = jvm.geopyspark.geotrellis.TemporalTiledRasterLayer
        option = jvm.scala.Option
        levels = {pyramid.apply(index)._1(): TiledRasterLayer(LayerType.SPACETIME, temporal_tiled_raster_layer(
            option.apply(pyramid.apply(index)._1()), pyramid.apply(index)._2())) for index in range(0, pyramid.size())}

        image_collection = GeotrellisTimeSeriesImageCollection(
            pyramid=gps.Pyramid(levels),
            service_registry=self._service_registry,
            metadata=CollectionMetadata(layer_metadata)
        )
        return image_collection.band_filter(band_indices) if band_indices else image_collection


def get_layer_catalog(service_registry: InMemoryServiceRegistry = None) -> GeoPySparkLayerCatalog:
    """
    Get layer catalog (from JSON files)
    """
    catalog_files = ConfigParams().layer_catalog_metadata_files
    logger.info("Reading layer catalog metadata from {f!r}".format(f=catalog_files[0]))
    metadata = read_json(catalog_files[0])
    if len(catalog_files) > 1:
        # Merge metadata recursively
        metadata = {l["id"]: l for l in metadata}
        for path in catalog_files[1:]:
            logger.info("Updating layer catalog metadata from {f!r}".format(f=path))
            updates = {l["id"]:l for l in read_json(path)}
            metadata = dict_merge_recursive(metadata, updates, overwrite=True)
        metadata = list(metadata.values())


    return GeoPySparkLayerCatalog(
        all_metadata=metadata,
        service_registry=service_registry or InMemoryServiceRegistry()
    )
