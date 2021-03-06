import json
import logging
import re
import subprocess
import sys
import tempfile
import traceback
import uuid
from pathlib import Path
from subprocess import CalledProcessError
from typing import List, Dict, Union
import shutil
from datetime import datetime
import os

import geopyspark as gps
import pkg_resources
from geopyspark import TiledRasterLayer, LayerType
from openeo.internal.process_graph_visitor import ProcessGraphVisitor
from openeo.metadata import CollectionMetadata, TemporalDimension
from openeo.util import dict_no_none, rfc3339
from openeo_driver import backend
from openeo_driver.backend import ServiceMetadata, BatchJobMetadata, OidcProvider, ErrorSummary
from openeo_driver.errors import (JobNotFinishedException, JobNotStartedException, ProcessGraphMissingException,
                                  OpenEOApiException, InternalException, ServiceUnsupportedException)
from py4j.java_gateway import JavaGateway
from py4j.protocol import Py4JJavaError

from openeogeotrellis.GeotrellisImageCollection import GeotrellisTimeSeriesImageCollection
from openeogeotrellis.configparams import ConfigParams
from openeogeotrellis.geotrellis_tile_processgraph_visitor import GeotrellisTileProcessGraphVisitor
from openeogeotrellis.job_registry import JobRegistry
from openeogeotrellis.layercatalog import get_layer_catalog
from openeogeotrellis.service_registry import (InMemoryServiceRegistry, ZooKeeperServiceRegistry,
                                               AbstractServiceRegistry, SecondaryService, ServiceEntity)
from openeogeotrellis.user_defined_process_repository import *
from openeogeotrellis.utils import normalize_date, kerberos, zk_client
from openeogeotrellis.traefik import Traefik

logger = logging.getLogger(__name__)


class GpsSecondaryServices(backend.SecondaryServices):
    """Secondary Services implementation for GeoPySpark backend"""

    def __init__(self, service_registry: AbstractServiceRegistry):
        self.service_registry = service_registry

    def service_types(self) -> dict:
        return {
            "WMTS": {
                "title": "Web Map Tile Service",
                "configuration": {
                    "version": {
                        "type": "string",
                        "description": "The WMTS version to use.",
                        "default": "1.0.0",
                        "enum": [
                            "1.0.0"
                        ]
                    },
                    "colormap": {
                        "type": "string",
                        "description": "The colormap to apply to single band layers",
                        "default": "YlGn"
                    }
                },
                "process_parameters": [
                    # TODO: we should at least have bbox and time range parameters here
                ],
                "links": [],
            }
        }

    def list_services(self, user_id: str) -> List[ServiceMetadata]:
        return list(self.service_registry.get_metadata_all(user_id).values())

    def service_info(self, user_id: str, service_id: str) -> ServiceMetadata:
        return self.service_registry.get_metadata(user_id=user_id, service_id=service_id)

    def remove_service(self, user_id: str, service_id: str) -> None:
        self.service_registry.stop_service(user_id=user_id, service_id=service_id)
        self._unproxy_service(service_id)

    def remove_services_before(self, upper: datetime) -> None:
        user_services = self.service_registry.get_metadata_all_before(upper)

        for user_id, service in user_services:
            self.service_registry.stop_service(user_id=user_id, service_id=service.id)
            self._unproxy_service(service.id)

    def _create_service(self, user_id: str, process_graph: dict, service_type: str, api_version: str,
                       configuration: dict) -> str:
        # TODO: reduce code duplication between this and start_service()
        from openeo_driver.ProcessGraphDeserializer import evaluate

        if service_type.lower() != 'wmts':
            raise ServiceUnsupportedException(service_type)

        service_id = str(uuid.uuid4())

        image_collection: GeotrellisTimeSeriesImageCollection = evaluate(
            process_graph,
            viewingParameters={'version': api_version, 'pyramid_levels': 'all'}
        )

        wmts_base_url = os.getenv('WMTS_BASE_URL_PATTERN', 'http://openeo.vgt.vito.be/openeo/services/%s') % service_id

        self.service_registry.persist(user_id, ServiceMetadata(
            id=service_id,
            process={"process_graph": process_graph},
            url=wmts_base_url + "/service/wmts",
            type=service_type,
            enabled=True,
            attributes={},
            configuration=configuration,
            created=datetime.utcnow()), api_version)

        secondary_service = self._wmts_service(image_collection, configuration, wmts_base_url)

        self.service_registry.register(service_id, secondary_service)
        self._proxy_service(service_id, secondary_service.host, secondary_service.port)

        return service_id

    def start_service(self, user_id: str, service_id: str) -> None:
        from openeo_driver.ProcessGraphDeserializer import evaluate

        service: ServiceEntity = self.service_registry.get(user_id=user_id, service_id=service_id)
        service_metadata: ServiceMetadata = service.metadata

        service_type = service_metadata.type
        process_graph = service_metadata.process["process_graph"]
        api_version = service.api_version
        configuration = service_metadata.configuration

        if service_type.lower() != 'wmts':
            raise ServiceUnsupportedException(service_type)

        image_collection: GeotrellisTimeSeriesImageCollection = evaluate(
            process_graph,
            viewingParameters={'version': api_version, 'pyramid_levels': 'all'}
        )

        wmts_base_url = os.getenv('WMTS_BASE_URL_PATTERN', 'http://openeo.vgt.vito.be/openeo/services/%s') % service_id

        secondary_service = self._wmts_service(image_collection, configuration, wmts_base_url)

        self.service_registry.register(service_id, secondary_service)
        self._proxy_service(service_id, secondary_service.host, secondary_service.port)

    def _wmts_service(self, image_collection, configuration: dict, wmts_base_url: str) -> SecondaryService:
        random_port = 0

        jvm = gps.get_spark_context()._gateway.jvm
        wmts = jvm.be.vito.eodata.gwcgeotrellis.wmts.WMTSServer.createServer(random_port, wmts_base_url)
        logger.info('Created WMTSServer: {w!s} ({u!s}/service/wmts, {p!r})'.format(w=wmts, u=wmts.getURI(), p=wmts.getPort()))

        if "colormap" in configuration:
            max_zoom = image_collection.pyramid.max_zoom
            min_zoom = min(image_collection.pyramid.levels.keys())
            reduced_resolution = max(min_zoom,max_zoom-4)
            if reduced_resolution not in image_collection.pyramid.levels:
                reduced_resolution = min_zoom
            histogram = image_collection.pyramid.levels[reduced_resolution].get_histogram()
            matplotlib_name = configuration.get("colormap", "YlGn")

            #color_map = gps.ColorMap.from_colors(breaks=[x for x in range(0,250)], color_list=gps.get_colors_from_matplotlib("YlGn"))
            color_map = gps.ColorMap.build(histogram, matplotlib_name)
            srdd_dict = {k: v.srdd.rdd() for k, v in image_collection.pyramid.levels.items()}
            wmts.addPyramidLayer("RDD", srdd_dict,color_map.cmap)
        else:
            srdd_dict = {k: v.srdd.rdd() for k, v in image_collection.pyramid.levels.items()}
            wmts.addPyramidLayer("RDD", srdd_dict)

        import socket
        # TODO what is this host logic about?
        host = [l for l in
                          ([ip for ip in socket.gethostbyname_ex(socket.gethostname())[2] if not ip.startswith("127.")][:1],
                           [[(s.connect(('8.8.8.8', 53)), s.getsockname()[0], s.close()) for s in
                             [socket.socket(socket.AF_INET, socket.SOCK_DGRAM)]][0][1]])
                          if l][0][0]

        return SecondaryService(host=host, port=wmts.getPort(), server=wmts)

    def restore_services(self):
        for user_id, service_metadata in self.service_registry.get_metadata_all_before(upper=datetime.max):
            if service_metadata.enabled:
                self.start_service(user_id=user_id, service_id=service_metadata.id)

    def _proxy_service(self, service_id, host, port):
        if not ConfigParams().is_ci_context:
            with zk_client() as zk:
                Traefik(zk).proxy_service(service_id, host, port)

    def _unproxy_service(self, service_id):
        if not ConfigParams().is_ci_context:
            with zk_client() as zk:
                Traefik(zk).unproxy_service(service_id)


class SingleNodeUDFProcessGraphVisitor(ProcessGraphVisitor):

    def __init__(self):
        super().__init__()
        self.udf_args = {}


    def enterArgument(self, argument_id: str, value):
        self.udf_args[argument_id] = value

    def constantArgument(self, argument_id: str, value):
        self.udf_args[argument_id] = value


class GeoPySparkBackendImplementation(backend.OpenEoBackendImplementation):

    def __init__(self):
        # TODO: do this with a config instead of hardcoding rules?
        self._service_registry = (
            InMemoryServiceRegistry() if ConfigParams().is_ci_context
            else ZooKeeperServiceRegistry()
        )

        user_defined_process_repository = (
            # choosing between DBs can be done in said config
            InMemoryUserDefinedProcessRepository() if ConfigParams().is_ci_context
            else ZooKeeperUserDefinedProcessRepository()
        )

        super().__init__(
            secondary_services=GpsSecondaryServices(service_registry=self._service_registry),
            catalog=get_layer_catalog(service_registry=self._service_registry),
            batch_jobs=GpsBatchJobs(),
            user_defined_processes=UserDefinedProcesses(user_defined_process_repository)
        )

    def health_check(self) -> str:
        from pyspark import SparkContext
        sc = SparkContext.getOrCreate()
        count = sc.parallelize([1, 2, 3]).map(lambda x: x * x).sum()
        return 'Health check: ' + str(count)

    def oidc_providers(self) -> List[OidcProvider]:
        return [
            OidcProvider(
                id="keycloak",
                # TODO EP-3377: move this to config or bootstrap script (and start using production URL?)
                issuer="https://sso-dev.vgt.vito.be/auth/realms/terrascope",
                scopes=["openid"],
                title="VITO Keycloak",
            )
        ]

    def file_formats(self) -> dict:
        return {
            "input": {
                "GeoJSON": {
                    "gis_data_types": ["vector"],
                    "parameters": {},
                }
            },
            "output": {
                "GTiff": {
                    "title": "GeoTiff",
                    "gis_data_types": ["raster"],
                    "parameters": {},
                },
                "CovJSON": {
                    "title": "CoverageJSON",
                    "gis_data_types": ["other"],  # TODO: also "raster", "vector", "table"?
                    "parameters": {},
                },
                "NetCDF": {
                    "title": "Network Common Data Form",
                    "gis_data_types": ["other","raster"],  # TODO: also "raster", "vector", "table"?
                    "parameters": {},
                },
                "JSON": {
                    "gis_data_types": ["raster"],
                    "parameters": {},
                }
            }
        }

    def load_disk_data(self, format: str, glob_pattern: str, options: dict, viewing_parameters: dict) -> object:
        if format != 'GTiff':
            raise NotImplementedError("The format is not supported by the backend: " + format)

        date_regex = options['date_regex']

        if glob_pattern.startswith("hdfs:"):
            kerberos()

        from_date = normalize_date(viewing_parameters.get("from", None))
        to_date = normalize_date(viewing_parameters.get("to", None))

        left = viewing_parameters.get("left", None)
        right = viewing_parameters.get("right", None)
        top = viewing_parameters.get("top", None)
        bottom = viewing_parameters.get("bottom", None)
        srs = viewing_parameters.get("srs", None)
        band_indices = viewing_parameters.get("bands")

        sc = gps.get_spark_context()

        gateway = JavaGateway(eager_load=True, gateway_parameters=sc._gateway.gateway_parameters)
        jvm = gateway.jvm

        extent = jvm.geotrellis.vector.Extent(float(left), float(bottom), float(right), float(top)) \
            if left is not None and right is not None and top is not None and bottom is not None else None

        pyramid = jvm.org.openeo.geotrellis.geotiff.PyramidFactory.from_disk(glob_pattern, date_regex) \
            .pyramid_seq(extent, srs, from_date, to_date)

        temporal_tiled_raster_layer = jvm.geopyspark.geotrellis.TemporalTiledRasterLayer
        option = jvm.scala.Option
        levels = {pyramid.apply(index)._1(): TiledRasterLayer(LayerType.SPACETIME, temporal_tiled_raster_layer(
            option.apply(pyramid.apply(index)._1()), pyramid.apply(index)._2())) for index in
                  range(0, pyramid.size())}

        metadata = CollectionMetadata(metadata={},dimensions=[TemporalDimension(name='t',extent=[])])

        image_collection = GeotrellisTimeSeriesImageCollection(
            pyramid=gps.Pyramid(levels),
            service_registry=self._service_registry,
            metadata=metadata
        )

        return image_collection.band_filter(band_indices) if band_indices else image_collection

    def visit_process_graph(self, process_graph: dict) -> ProcessGraphVisitor:
        return GeoPySparkBackendImplementation.accept_process_graph(process_graph)

    @classmethod
    def accept_process_graph(cls, process_graph):
        if len(process_graph) == 1 and next(iter(process_graph.values())).get('process_id') == 'run_udf':
            return SingleNodeUDFProcessGraphVisitor().accept_process_graph(process_graph)
        return GeotrellisTileProcessGraphVisitor().accept_process_graph(process_graph)

    def summarize_exception(self, error: Exception) -> Union[ErrorSummary, Exception]:
        if isinstance(error, Py4JJavaError):
            java_exception = error.java_exception

            while java_exception.getCause() is not None and java_exception != java_exception.getCause():
                java_exception = java_exception.getCause()

            java_exception_class_name = java_exception.getClass().getName()
            java_exception_message = java_exception.getMessage()

            no_data_found = (java_exception_class_name == 'java.lang.AssertionError'
                             and "Cannot stitch empty collection" in java_exception_message)

            is_client_error = java_exception_class_name == 'java.lang.IllegalArgumentException' or no_data_found
            summary = "Cannot construct an image because the given boundaries resulted in an empty image collection" if no_data_found else java_exception_message

            return ErrorSummary(error, is_client_error, summary)

        return error


class GpsBatchJobs(backend.BatchJobs):
    _OUTPUT_ROOT_DIR = Path("/data/projects/OpenEO/")

    def create_job(self, user_id: str, process: dict, api_version: str, job_options: dict = None) -> BatchJobMetadata:
        job_id = str(uuid.uuid4())
        with JobRegistry() as registry:
            job_info = registry.register(
                job_id=job_id,
                user_id=user_id,
                api_version=api_version,
                specification=dict_no_none(
                    process_graph=process["process_graph"],
                    job_options=job_options,
                )
            )
        return BatchJobMetadata(
            id=job_id, process=process, status=job_info["status"],
            created=rfc3339.parse_datetime(job_info["created"]), job_options=job_options
        )

    def get_job_info(self, job_id: str, user_id: str) -> BatchJobMetadata:
        with JobRegistry() as registry:
            job_info = registry.get_job(job_id, user_id)
            return registry.job_info_to_metadata(job_info)

    def get_user_jobs(self, user_id: str) -> List[BatchJobMetadata]:
        with JobRegistry() as registry:
            return [
                registry.job_info_to_metadata(job_info)
                for job_info in registry.get_user_jobs(user_id)
            ]

    def _get_job_output_dir(self, job_id: str) -> Path:
        return GpsBatchJobs._OUTPUT_ROOT_DIR / job_id

    def start_job(self, job_id: str, user_id: str):
        from pyspark import SparkContext

        with JobRegistry() as registry:
            job_info = registry.get_job(job_id, user_id)
            api_version = job_info.get('api_version')

            # restart logic
            current_status = job_info['status']
            if current_status in ['queued', 'running']:
                return
            elif current_status != 'created':
                registry.mark_ongoing(job_id, user_id)
                registry.set_application_id(job_id, user_id, None)
                registry.set_status(job_id, user_id, 'created')

            spec = json.loads(job_info['specification'])
            extra_options = spec.get('job_options', {})

            driver_memory = extra_options.get("driver-memory", "12G")
            driver_memory_overhead = extra_options.get("driver-memoryOverhead", "2G")
            executor_memory = extra_options.get("executor-memory", "2G")
            executor_memory_overhead = extra_options.get("executor-memoryOverhead", "2G")
            driver_cores =extra_options.get("driver-cores", "5")
            executor_cores =extra_options.get("executor-cores", "2")
            queue = extra_options.get("queue", "default")

            kerberos()

            conf = SparkContext.getOrCreate().getConf()
            principal, key_tab = conf.get("spark.yarn.principal"), conf.get("spark.yarn.keytab")

            script_location = pkg_resources.resource_filename('openeogeotrellis.deploy', 'submit_batch_job.sh')

            with tempfile.NamedTemporaryFile(mode="wt",
                                             encoding='utf-8',
                                             dir=GpsBatchJobs._OUTPUT_ROOT_DIR,
                                             prefix="{j}_".format(j=job_id),
                                             suffix=".in") as temp_input_file:
                temp_input_file.write(job_info['specification'])
                temp_input_file.flush()

                args = [script_location,
                        "OpenEO batch job {j} user {u}".format(j=job_id, u=user_id),
                        temp_input_file.name,
                        str(self._get_job_output_dir(job_id)),
                        "out",  # TODO: how support multiple output files?
                        "log",
                        "metadata"]

                if principal is not None and key_tab is not None:
                    args.append(principal)
                    args.append(key_tab)
                else:
                    args.append("no_principal")
                    args.append("no_keytab")

                args.append(user_id)

                if api_version:
                    args.append(api_version)
                else:
                    args.append("0.4.0")

                args.append(driver_memory)
                args.append(executor_memory)
                args.append(executor_memory_overhead)
                args.append(driver_cores)
                args.append(executor_cores)
                args.append(driver_memory_overhead)
                args.append(queue)

                try:
                    logger.info("Submitting job: {a!r}".format(a=args))
                    output_string = subprocess.check_output(args, stderr=subprocess.STDOUT, universal_newlines=True)
                except CalledProcessError as e:
                    logger.exception(e)
                    logger.error(e.stdout)
                    logger.error(e.stderr)
                    raise e

            try:
                # note: a job_id is returned as soon as an application ID is found in stderr, not when the job is finished
                logger.info(output_string)
                application_id = self._extract_application_id(output_string)
                print("mapped job_id %s to application ID %s" % (job_id, application_id))

                registry.set_application_id(job_id, user_id, application_id)
            except _BatchJobError as e:
                traceback.print_exc(file=sys.stderr)
                # TODO: why reraise as CalledProcessError?
                raise CalledProcessError(1, str(args), output=output_string)

    @staticmethod
    def _extract_application_id(stream) -> str:
        regex = re.compile(r"^.*Application report for (application_\d{13}_\d+)\s\(state:.*", re.MULTILINE)
        match = regex.search(stream)
        if match:
            return match.group(1)
        else:
            raise _BatchJobError(stream)

    def get_results(self, job_id: str, user_id: str) -> Dict[str, str]:
        job_info = self.get_job_info(job_id=job_id, user_id=user_id)
        if job_info.status != 'finished':
            raise JobNotFinishedException
        return {
            "out": str(self._get_job_output_dir(job_id=job_id))
        }

    def get_results_metadata(self, job_id: str, user_id: str) -> dict:
        metadata_file = self._get_job_output_dir(job_id) / "metadata"

        try:
            with open(metadata_file) as f:
                return json.load(f)
        except FileNotFoundError:
            logger.warning("Could not derive result metadata from %s", metadata_file, exc_info=True)

        return {}

    def get_log_entries(self, job_id: str, user_id: str, offset: str) -> List[dict]:
        # will throw if job doesn't match user
        job_info = self.get_job_info(job_id=job_id, user_id=user_id)
        if job_info.status in ['created', 'queued']:
            return []

        log_file = self._get_job_output_dir(job_id) / "log"
        with log_file.open('r') as f:
            log_file_contents = f.read()
        # TODO: provide log line per line, with correct level?
        return [
            {
                'id': "0",
                'level': 'error',
                'message': log_file_contents
            }
        ]

    def cancel_job(self, job_id: str, user_id: str):
        with JobRegistry() as registry:
            application_id = registry.get_job(job_id, user_id)['application_id']
        if application_id:
            kill_spark_job = subprocess.run(
                ["yarn", "application", "-kill", application_id],
                timeout=20,
                check=True,
                universal_newlines=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT  # combine both output streams into one
            )

            logger.debug("Killed corresponding Spark job for job {j}: {a!r}".format(j=job_id, a=kill_spark_job.args))
        else:
            raise InternalException("Application ID unknown for job {j}".format(j=job_id))

    def delete_job(self, job_id: str, user_id: str):
        self._delete_job(job_id, user_id, propagate_errors=False)

    def _delete_job(self, job_id: str, user_id: str, propagate_errors: bool):
        try:
            self.cancel_job(job_id, user_id)
        except InternalException:  # job never started, not an error
            pass
        except CalledProcessError as e:
            if e.returncode == 255 and "doesn't exist in RM" in e.stdout:  # already finished and gone, not an error
                pass
            elif propagate_errors:
                raise
            else:
                logger.warning("Unable to kill corresponding Spark job for job {j}: {a!r}\n{o}".format(j=job_id, a=e.cmd,
                                                                                                       o=e.stdout),
                               exc_info=e)

        job_dir = self._get_job_output_dir(job_id)

        try:
            shutil.rmtree(job_dir)
        except FileNotFoundError as e:  # nothing to delete, not an error
            pass
        except Exception as e:
            if propagate_errors:
                raise
            else:
                logger.warning("Could not delete {p}".format(p=job_dir), exc_info=e)

        with JobRegistry() as registry:
            registry.delete(job_id, user_id)

        logger.info("Deleted job {u}/{j}".format(u=user_id, j=job_id))

    def delete_jobs_before(self, upper: datetime) -> None:
        with JobRegistry() as registry:
            jobs_before = registry.get_all_jobs_before(upper)

        for job_info in jobs_before:
            self._delete_job(job_id=job_info['job_id'], user_id=job_info['user_id'], propagate_errors=True)


class _BatchJobError(Exception):
    pass


class UserDefinedProcesses(backend.UserDefinedProcesses):
    _valid_process_graph_id = re.compile(r"^\w+$")

    def __init__(self, user_defined_process_repository: UserDefinedProcessRepository):
        self._repo = user_defined_process_repository

    def get(self, user_id: str, process_id: str) -> Union[UserDefinedProcessMetadata, None]:
        return self._repo.get(user_id, process_id)

    def get_for_user(self, user_id: str) -> List[UserDefinedProcessMetadata]:
        return self._repo.get_for_user(user_id)

    def save(self, user_id: str, process_id: str, spec: dict) -> None:
        self._validate(spec, process_id)
        self._repo.save(user_id, spec)

    def delete(self, user_id: str, process_id: str) -> None:
        self._repo.delete(user_id, process_id)

    def _validate(self, spec: dict, process_id: str) -> None:
        if 'process_graph' not in spec:
            raise ProcessGraphMissingException

        if not self._valid_process_graph_id.match(process_id):
            raise OpenEOApiException(
                status_code=400,
                message="Invalid process_graph_id {i}, must match {p}".format(i=process_id,
                                                                              p=self._valid_process_graph_id.pattern))

        spec['id'] = process_id
