#!/usr/bin/env python3
"""
Where things live on this host. Nothing in the dashboard may assume one
distribution's layout: tools are looked up on PATH (plus the sbin
directories a service's PATH can lack), and the application's own files are
found relative to this module, wherever it was installed.
"""

import os
import shutil

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("CLABD_DATA_DIR", "/var/lib/clab-dashboard")

_EXTRA = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def find(*names):
    """Absolute path of the first of names found; the bare first name when
    none is (the call then fails with a clear 'not found' when used)."""
    path = os.environ.get("PATH", "") + ":" + _EXTRA
    for n in names:
        p = shutil.which(n, path=path)
        if p:
            return p
    return names[0]
