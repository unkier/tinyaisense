from importlib.metadata import version

import tinyaisense_sim


def test_sim_package_reports_its_distribution_version() -> None:
    assert tinyaisense_sim.__version__ == version("tinyaisense-sim")
