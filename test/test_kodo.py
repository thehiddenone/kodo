from importlib.metadata import version

import kodo


def get_base_version(ver: str) -> str:
    pos = ver.rfind("b")
    return ver[:pos]


def test_version() -> None:
    assert get_base_version(kodo.__version__) == get_base_version(version("kodo"))
