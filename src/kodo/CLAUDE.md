# This file contains general coding guidelines for Claude Code

## Class definition

Always model a class by splitting it into private and public members. Public members can be properties and methods, private members can be variables and methods.

Always name-mangle private members of a class. If necessary, provide access to private member variables through read-only properties. You MUST avoid implementation of member variables that need to be modified outside of the class as this is indicative of bad design/architecture.

Never use name-mangled symbols from outside the class.

Add Google Style docstrings to `__init__()`, public methods, and the class itself.

Example:

```python
class Example:
    """This is an example of class definition."""
    __var1: int
    __var2: str

    def __init__(self, var1: int) -> None:
        """Docstring for constructor

        Args:
            var1 (int): bla bla bla
        """
        self.__var1 = var1
        self.__var2 = str(var1 + 1)

    @property
    def var1(self) -> int:
        return self.__var1

    def method(self, arg: str) -> str:
        """Docstring for method

        Args:
            var1 (int): bla bla bla

        Returns:
            str: bla bla bla

        """
        return self.__var2 + arg

    def __private_method(self) -> None:
        pass

```

## Module definition

Always create __init__.py and py.typed in every python module. Always use relative imports from files within the module. Never use relative imports when importing from other packages.

Always prepend underscore to python files inside a package.

Always define `__all__` in `__init__.py`

Always put ```from __future__ import annotations``` at the top of each python source code file except for `__init__.py` files.

Always provide a Google Style docstring for a module.

Never use star import: ```from somewhere import *  # Never do that!```

Example:

```python
"""This is an example module"""

from ._local_file import Something
from another_package.subpackage import Other

__all__ = [
    'Something',
    'Other',
]
```

## Type hints

You MUST NOT use `Optional`, `Any`, `Dict`, `List`, `TYPE_CHECKING` etc. No loose type safety, no outdated classes.

You MUST use `object` instead of `Any`, and use `cast` or `isinstance` where necessary.

You MUST use `typename | None` instead of `Optional`.

## Test implementation

Design tests and classes such that unit tests validate behavior, not implementation details. It should not matter how many times a mock was called if the behavior is correct.

Never build tests that rely on private methods or variables.

Test private methods through behavoir of public methods.
