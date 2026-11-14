# -*- coding: utf-8 -*-
"""
Master Management Subsystem.
Enables user identity binding, persistent appellation configuration,
group isolation, and context-menu interactions.
"""

from master.manager import MasterManager, MasterRecord, default_master_manager

__all__ = ["MasterManager", "MasterRecord", "default_master_manager"]
