import time
from collections import defaultdict, deque

import requests

import ray
from ray.experimental.serve.kv_store_service import KVStoreProxyActor
from ray.experimental.serve.queues import CentralizedQueuesActor
from ray.experimental.serve.utils import logger
from ray.experimental.serve.server import HTTPActor
from ray.experimental.serve.metric import (MetricMonitor,
                                           start_metric_monitor_loop)

# TODO(simon): Global state currently is designed to resides in the driver
#     process. In the next iteration, we will move all mutable states into
#     two actors: (1) namespaced key-value store backed by persistent store
#     (2) actor supervisors holding all actor handles and is responsible
#     for new actor instantiation and dead actor termination.

LOG_PREFIX = "[Global State] "


class GlobalState:
    """Encapsulate all global state in the serving system.

    Warning:
        Currently the state resides inside driver process. The state will be
        moved into a key value stored service AND a supervisor service.
    """

    def __init__(self):
        #: actor handle to KV store actor
        self.kv_store_actor_handle = None
        #: actor handle to HTTP server
        self.http_actor_handle = None
        #: actor handle the router actor
        self.router_actor_handle = None

        #: Set[str] list of backend names, used for deduplication
        self.registered_backends = set()
        #: Set[str] list of service endpoint names, used for deduplication
        self.registered_endpoints = set()

        #: Mapping of endpoints -> a stack of traffic policy
        self.policy_action_history = defaultdict(deque)

        #: Backend creaters. Mapping backend_tag -> callable creator
        self.backend_creators = dict()
        #: Number of replicas per backend.
        #  Mapping backend_tag -> deque(actor_handles)
        self.backend_replicas = defaultdict(deque)

        #: HTTP address. Currently it's hard coded to localhost with port 8000
        #  In future iteration, HTTP server will be started on every node and
        #  use random/available port in a pre-defined port range. TODO(simon)
        self.http_address = ""

        #: Metric monitor handle
        self.metric_monitor_handle = None

    def init_api_server(self):
        logger.info(LOG_PREFIX + "Initalizing routing table")
        self.kv_store_actor_handle = KVStoreProxyActor.remote()
        logger.info((LOG_PREFIX + "Health checking routing table {}").format(
            ray.get(self.kv_store_actor_handle.get_request_count.remote())), )

    def init_http_server(self):
        logger.info(LOG_PREFIX + "Initializing HTTP server")
        self.http_actor_handle = HTTPActor.remote(self.kv_store_actor_handle,
                                                  self.router_actor_handle)
        self.http_actor_handle.run.remote(host="0.0.0.0", port=8000)
        self.http_address = "http://localhost:8000"

    def init_router(self):
        logger.info(LOG_PREFIX + "Initializing queuing system")
        self.router_actor_handle = CentralizedQueuesActor.remote()
        self.router_actor_handle.register_self_handle.remote(
            self.router_actor_handle)

    def init_metric_monitor(self, gc_window_seconds=3600):
        logger.info(LOG_PREFIX + "Initializing metric monitor")
        self.metric_monitor_handle = MetricMonitor.remote(gc_window_seconds)
        start_metric_monitor_loop.remote(self.metric_monitor_handle)
        self.metric_monitor_handle.add_target.remote(self.router_actor_handle)

    def wait_until_http_ready(self, num_retries=5, backoff_time_s=1):
        http_is_ready = False
        retries = num_retries

        while not http_is_ready:
            try:
                resp = requests.get(self.http_address)
                assert resp.status_code == 200
                http_is_ready = True
            except Exception:
                pass

            logger.debug((LOG_PREFIX + "Checking if HTTP server is ready."
                          "{} retries left.").format(retries))
            time.sleep(backoff_time_s)
            # Exponential backoff
            backoff_time_s *= 2
            retries -= 1
            if retries == 0:
                raise Exception(
                    "HTTP server not ready after {} retries.".format(
                        num_retries))
