#!/usr/bin/env python3
"""
Cache module for autoscaler
"""

from .redis_cache import RedisCache

__all__ = ["RedisCache"]