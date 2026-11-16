import pytest

from trag import demo
from trag.parse import Event


@pytest.fixture(scope="session")
def demo_logs(tmp_path_factory):
    """The built-in sample incident, written once for the whole test run."""
    directory = tmp_path_factory.mktemp("demo")
    app, access = demo.write(str(directory))
    return {"app": app, "access": access}


def ev(ts, message, level="info", **fields):
    e = Event(ts, level, "", message, fields)
    e.line_no = 1
    return e
