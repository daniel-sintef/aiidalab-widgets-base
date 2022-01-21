# -*- coding: utf-8 -*-
try:
    from dunamai import Version, get_version

    __version__ = Version.from_git().serialize()
except RuntimeError:
    __version__ = get_version("aiidalab-widgets-base").serialize()
except ImportError:
    __version__ = "1.2.0"
