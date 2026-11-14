"""Run all repository tests away from the developer's active bot and accounts."""
from pathlib import Path

import pytest

from testing_sandbox import install_guards, is_sandbox, run_isolated


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config):
    if not is_sandbox(Path(__file__).resolve().parent):
        capture = config.pluginmanager.get_plugin("capturemanager")
        if capture is not None:
            capture.suspend_global_capture(in_=True)
        return run_isolated(config.invocation_params.args, config.invocation_params.dir)
    return None


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    if is_sandbox(Path(__file__).resolve().parent):
        install_guards()
    else:
        raise pytest.UsageError("Tests must be bootstrapped into the disposable qqbot checkout.")


@pytest.fixture(autouse=True)
def require_offline_test(request):
    state = install_guards()
    count = state["blocked_count"]
    yield
    if request.node.path.name != "test_test_isolation.py" and state["blocked_count"] > count:
        pytest.fail("Test attempted live access; replace its external dependency with a fixture: "
                    + str(state["blocked"][count:]))

