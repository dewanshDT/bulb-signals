import pytest


def pytest_addoption(parser):
    parser.addoption("--hardware", action="store_true", help="run tests that drive the real bulbs")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--hardware"):
        return
    skip = pytest.mark.skip(reason="needs --hardware")
    for item in items:
        if "hardware" in item.keywords:
            item.add_marker(skip)
