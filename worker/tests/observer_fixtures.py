"""Explicit offline observer construction for tests, never production wiring."""
from momentum.observation import ObservationWorker
from momentum.paper_run_logging import DisabledPaperRunLogger


def offline_observation_worker(*args, **kwargs):
    # The publisher and paper-run logger have independent transports. Production
    # construction validates config and creates the logger but performs no I/O.
    # Replace that logger before __enter__ can start a cloud logging session.
    worker = ObservationWorker(*args, **kwargs)
    worker.paper_run_logger = DisabledPaperRunLogger(reason="offline_fixture")
    return worker
