import time

from Queue import Queue
from urllib import urlencode
from threading import Lock, current_thread

from django.conf import settings

from graphite.http_pool import http
from graphite.readers import FetchInProgress
from graphite.logger import log
from graphite.util import unpickle
from graphite.worker_pool.pool import get_pool


class RemoteReader(object):
    __slots__ = (
        'store',
        'metric_path',
        'intervals',
        'bulk_query',
        'connection')
    inflight_lock = Lock()

    def __init__(self, store, node_info, bulk_query=None):
        self.store = store
        self.metric_path = node_info.get(
            'path') or node_info.get('metric_path')
        self.intervals = node_info['intervals']
        self.bulk_query = bulk_query or (
            [self.metric_path] if self.metric_path else []
        )
        self.connection = None

    def __repr__(self):
        return '<RemoteReader[%x]: %s>' % (id(self), self.store.host)

    @staticmethod
    def _log(msg, logger):
        logger(('thread %s at %fs ' %
                (current_thread().name, time.time())) + msg)

    @classmethod
    def log_debug(cls, msg):
        if settings.DEBUG:
            cls._log(msg, log.info)

    @classmethod
    def log_error(cls, msg):
        cls._log(msg, log.exception)

    def get_intervals(self):
        return self.intervals

    def fetch(self, startTime, endTime, now=None, requestContext=None):
        seriesList = self.fetch_list(startTime, endTime, now, requestContext)

        def _fetch(seriesList):
            if seriesList is None:
                return None

            for series in seriesList:
                if series['name'] == self.metric_path:
                    time_info = (
                        series['start'],
                        series['end'],
                        series['step'])
                    return (time_info, series['values'])

            return None

        if isinstance(seriesList, FetchInProgress):
            return FetchInProgress(lambda: _fetch(seriesList.waitForResults()))

        return _fetch(seriesList)

    def fetch_list(self, startTime, endTime, now=None, requestContext=None):
        t = time.time()

        query_params = [
            ('format', 'pickle'),
            ('local', '1'),
            ('noCache', '1'),
            ('from', str(int(startTime))),
            ('until', str(int(endTime)))
        ]

        if len(self.bulk_query) < 1:
            return []

        for target in self.bulk_query:
            query_params.append(('target', target))

        if now is not None:
            query_params.append(('now', str(int(now))))

        query_string = urlencode(query_params)
        urlpath = '/render/'
        url = "%s://%s%s" % ('https' if settings.INTRACLUSTER_HTTPS else 'http',
                             self.store.host, urlpath)
        headers = requestContext.get(
            'forwardHeaders') if requestContext else None

        cacheKey = "%s?%s" % (url, query_string)

        if requestContext is not None and 'inflight_requests' in requestContext and cacheKey in requestContext[
                'inflight_requests']:
            self.log_debug(
                "RemoteReader:: Returning cached FetchInProgress %s?%s" %
                (url, query_string))
            return requestContext['inflight_requests'][cacheKey]

        if requestContext is None or 'inflight_locks' not in requestContext or cacheKey not in requestContext[
                'inflight_locks']:
            with self.inflight_lock:
                self.log_debug(
                    "RemoteReader:: Got global lock %s?%s" %
                    (url, query_string))
                if requestContext is None:
                    requestContext = {}
                if 'inflight_locks' not in requestContext:
                    requestContext['inflight_locks'] = {}
                if 'inflight_requests' not in requestContext:
                    requestContext['inflight_requests'] = {}
                if cacheKey not in requestContext['inflight_locks']:
                    self.log_debug(
                        "RemoteReader:: Creating lock %s?%s" %
                        (url, query_string))
                    requestContext['inflight_locks'][cacheKey] = Lock()
            self.log_debug(
                "RemoteReader:: Released global lock %s?%s" %
                (url, query_string))

        cacheLock = requestContext['inflight_locks'][cacheKey]

        with cacheLock:
            self.log_debug(
                "RemoteReader:: got url lock %s?%s" %
                (url, query_string))

            if cacheKey in requestContext['inflight_requests']:
                self.log_debug(
                    "RemoteReader:: Returning cached FetchInProgress %s?%s" %
                    (url, query_string))
                return requestContext['inflight_requests'][cacheKey]

            q = Queue()
            if settings.USE_WORKER_POOL:
                get_pool().apply_async(
                    func=self._fetch,
                    args=[url, query_string, query_params, headers],
                    callback=lambda x: q.put(x),
                )
            else:
                q.put(
                    self._fetch(url, query_string, query_params, headers),
                )

            def retrieve():
                with retrieve.lock:
                    # if the result is known we return it directly
                    if hasattr(retrieve, '_result'):
                        results = getattr(retrieve, '_result')
                        self.log_debug(
                            'RemoteReader:: retrieve completed (cached) %s' %
                            (', '.join([result['path'] for result in results])),
                        )
                        return results

                    # otherwise we get it from the queue and keep it for later
                    results = q.get(block=True)

                    for i in range(len(results)):
                        results[i]['path'] = results[i]['name']

                    if not results:
                        self.log_debug(
                            'RemoteReader:: retrieve has received no results')

                    setattr(retrieve, '_result', results)
                    self.log_debug(
                        'RemoteReader:: retrieve completed %s' %
                        (', '.join([result['path'] for result in results])),
                    )
                    return results

            self.log_debug(
                'RemoteReader:: Storing FetchInProgress with cacheKey {cacheKey}' .format(
                    cacheKey=cacheKey), )
            retrieve.lock = Lock()
            data = FetchInProgress(retrieve)
            requestContext['inflight_requests'][cacheKey] = data

        self.log_debug(
            "RemoteReader:: Returning %s?%s in %fs" %
            (url, query_string, time.time() - t))
        return data

    def _fetch(self, url, query_string, query_params, headers):
        self.log_debug(
            "RemoteReader:: Starting to execute _fetch %s?%s" %
            (url, query_string))
        try:
            self.log_debug(
                "ReadResult:: Requesting %s?%s" %
                (url, query_string))
            result = http.request(
                'POST' if settings.REMOTE_STORE_USE_POST else 'GET',
                url,
                fields=query_params,
                headers=headers,
                timeout=settings.REMOTE_FETCH_TIMEOUT,
            )

            if result.status != 200:
                self.store.fail()
                self.log_error(
                    "ReadResult:: Error response %d from %s?%s" %
                    (result.status, url, query_string))
                data = []
            else:
                data = unpickle.loads(result.data)
        except Exception as err:
            self.store.fail()
            self.log_error(
                "ReadResult:: Error requesting %s?%s: %s" %
                (url, query_string, err))
            data = []

        self.log_debug(
            "RemoteReader:: Completed _fetch %s?%s" %
            (url, query_string))
        return data
