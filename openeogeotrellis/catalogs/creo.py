import re
from datetime import datetime, time
from pathlib import Path

import requests
from shapely.geometry import Polygon

from openeogeotrellis.catalogs.base import CatalogClientBase, CatalogConstantsBase, CatalogEntryBase, CatalogStatus
from openeogeotrellis.catalogs.creo_ordering import CreoOrder


class CatalogConstants(CatalogConstantsBase):
    missionSentinel2 = 'Sentinel2'
    level1C = 'LEVEL1C'
    level2A = 'LEVEL2A'


class CatalogEntry(CatalogEntryBase):
    # product_id expected as one of:
    #   /eodata/Sentinel-2/MSI/L2A/2019/11/17/S2B_MSIL2A_20191117T105229_N0213_R051_T31UET_20191117T134337.SAFE
    #   S2B_MSIL2A_20191117T105229_N0213_R051_T31UET_20191117T134337
    def __init__(self, product_id, status, s3_bucket=None, s3_key=None):
        prarr = product_id.replace('.SAFE', '').split('/')
        self._product_id = prarr[-1]
        self._tile_id = re.split('_T([0-9][0-9][A-Z][A-Z][A-Z])_', product_id)[1].split('_')[0]
        self._s3_bucket = s3_bucket
        self._s3_key = s3_key
        if self._s3_bucket is None:
            if len(prarr) == 1:
                self._s3_bucket = 'EODATA'
            else:
                self._s3_bucket = prarr[1].upper()
        if self._s3_key is None:
            if len(prarr) == 1:
                self._s3_key = '/'.join([
                    'Sentinel-2',
                    self._product_id[4:7],  # MSI
                    self._product_id[7:10],  # L2A
                    self._product_id[11:15],  # 2019
                    self._product_id[15:17],  # 11
                    self._product_id[17:19],  # 17
                    self._product_id
                ]) + '.SAFE'
            else:
                self._s3_key = '/'.join(prarr[2:]) + '.SAFE'
        self._status = status
        self.relfilerelpathbuffer = None  # in order not to request the xml every time for a band, this is prepared the first time

    def __str__(self):
        return self._product_id

    def getProductId(self):
        return self._product_id

    def getS3Bucket(self):
        return self._s3_bucket

    def getS3Key(self):
        return self._s3_key

    def getTileId(self):
        return self._tile_id

    def getStatus(self):
        return self._status

    def getFileRelPath(self, s3connection, band, resolution):
        if self.relfilerelpathbuffer is None:
            self.relfilerelpathbuffer = s3connection.get_band_filename(self, band, resolution).replace('_' + band + '_',
                                                                                                       '_@BAND@_')
        return self.relfilerelpathbuffer.replace('@BAND@', band)


class CatalogClient(CatalogClientBase):

    @staticmethod
    def _build_polygon(ulx, uly, brx, bry):
        return Polygon([(ulx, uly), (brx, uly), (brx, bry), (ulx, bry), (ulx, uly)])

    @staticmethod
    def _parse_product_ids(response):
        result = []
        for hit in response['features']:
            if hit['properties']['status'] == 0 or hit['properties']['status'] == 34 or hit['properties'][
                'status'] == 37:
                result.append(
                    CatalogEntry(hit['properties']['productIdentifier'].replace('.SAFE', ''), CatalogStatus.AVAILABLE))
            else:
                result.append(
                    CatalogEntry(hit['properties']['productIdentifier'].replace('.SAFE', ''), CatalogStatus.ORDERABLE))
        return result

    def __init__(self, mission, level):
        super().__init__(mission, level)
        self.itemsperpage = 100
        self.maxpages = 100  # elasticsearch has a 10000 limit on the paged search

    def catalogEntryFromProductId(self, product_id):
        return CatalogEntry(product_id, CatalogStatus.AVAILABLE)

    def _query_page(self, start_date, end_date,
                    tile_id,
                    ulx, uly, brx, bry,
                    cldPrcnt,
                    from_index):

        query_params = [('processingLevel', self.level),
                        ('startDate', start_date.isoformat()),
                        ('cloudCover', '[0,' + str(int(cldPrcnt)) + ']'),
                        ('page', str(from_index)),
                        ('maxRecords', str(self.itemsperpage)),
                        ('sortParam', 'startDate'),
                        ('sortOrder', 'ascending'),
                        ('status', 'all'),
                        ('dataset', 'ESA-DATASET')]

        # optional parameters
        if end_date is not None:
            query_params.append(('completionDate', end_date.isoformat()))
        if tile_id is None:
            polygon = CatalogClient._build_polygon(ulx, uly, brx, bry)
            query_params.append(('geometry', polygon.wkt))
        else:
            query_params.append(('productIdentifier', '%_T' + tile_id + '_%'))

        response = requests.get('https://finder.creodias.eu/resto/api/collections/' + self.mission + '/search.json',
                                params=query_params)

        # procurl=response.request.url

        # when the request fails raise HTTP error
        try:
            response = response.json()
        except ValueError:
            response.raise_for_status()

        # for i in response['features']: self.logger.info(i['properties']['productIdentifier'])
        # self.logger.info(procurl)
        # self.logger.info(response['properties']['itemsPerPage'])
        # self.logger.info(response['properties']['totalResults'])

        self.logger.debug('Paged catalogs query returned %d results', response['properties']['itemsPerPage'])
        return response

    def _query_per_tile(self, start_date, end_date,
                        tile_id,
                        ulx, uly, brx, bry,
                        cldPrcnt):

        result = []

        # get first page
        response = self._query_page(start_date, end_date, tile_id, ulx, uly, brx, bry, cldPrcnt, 1)
        # since int(response['properties']['totalResults']) does not always return exact count, therefore need to query until features is empty
        self.logger.debug("Hits in catalogs: " + str(response['properties']['totalResults']) + " exact: " + str(
            response['properties']['exactCount']))
        # if total_hits>10000:
        #    raise Exception("Total hits larger than 10000, which is not supported by paging: either split your job to multiple smaller or implement scroll or search_after.")
        for i in range(self.maxpages):
            response = self._query_page(start_date, end_date, tile_id, ulx, uly, brx, bry, cldPrcnt, i + 1)
            chunk = CatalogClient._parse_product_ids(response)
            if len(chunk) == 0: break
            result = result + chunk
        if len(result) >= self.itemsperpage * self.maxpages:
            raise Exception(
                "Total hits larger than 10000, which is not supported by paging: either split your job to multiple smaller or implement scroll or search_after.")

        return result

    def query(self, start_date, end_date,
              tile_ids=None,
              ulx=-180, uly=90, brx=180, bry=-90,
              cldPrcnt=100.):

        result = []
        if tile_ids is None:
            result = result + self._query_per_tile(start_date, end_date, None, ulx, uly, brx, bry, cldPrcnt)
        else:
            for itileid in tile_ids:
                result = result + self._query_per_tile(start_date, end_date, itileid, ulx, uly, brx, bry, cldPrcnt)

        self.logger.info('Number of products found: ' + str(len(result)))

        return result

    def query_product_paths(self, start_date, end_date, ulx, uly, brx, bry):
        products = self.query(start_date, datetime.combine(end_date, time.max), ulx=ulx, uly=uly, brx=brx, bry=bry)
        return [str(Path("/", p.getS3Bucket().lower(), p.getS3Key())) for p in products]

    def order(self, entries):
        tag = str(len(entries)) + 'products'
        if entries is not None:
            self.logger.info("Catalog found %d products (%d available, %d orderable, %d not-found)" % (
                len(entries),
                len(list(filter(lambda i: i.getStatus() == CatalogStatus.AVAILABLE, entries))),
                len(list(filter(lambda i: i.getStatus() == CatalogStatus.ORDERABLE, entries))),
                len(list(filter(lambda i: i.getStatus() == CatalogStatus.NOT_FOUND, entries)))
            ))
        for i in entries: self.logger.debug(str(i.getStatus()) + " -> " + i.getProductId())
        order = CreoOrder().order(entries, tag)
        return order

    def count(self, start_date, end_date,
              tile_ids=None,
              ulx=-180, uly=90, brx=180, bry=-90,
              cldPrcnt=100.):

        # ugly, but totalresults do not always return the exact number
        return len(self.query(start_date, end_date, tile_ids, ulx, uly, brx, bry, cldPrcnt))
