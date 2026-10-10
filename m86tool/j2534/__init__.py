"""Build a shared library exposing J2534 API for Python/ctypes testing.

This is handy if you want to test with a program expecting a DLL.
Build on Linux: `gcc -shared -fPIC -o libj2534m86.so -I. -x c -`
(but easier: just use Python ctypes directly against this module).
"""

from __future__ import annotations
from . import wrap  # noqa
