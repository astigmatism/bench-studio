import pytest


@pytest.fixture(autouse=True)
def fresh_router_checks():
    """Scheduler check timestamps are process-global; isolate each test."""
    from studio import runner

    runner.CHECKED.clear()
    yield
    runner.CHECKED.clear()
