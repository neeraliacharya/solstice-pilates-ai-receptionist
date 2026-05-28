import time
from functools import wraps
from typing import Any, Callable, Dict, Tuple

# Global registry to allow clearing all caches on mutation
_caches: list[Dict[str, Tuple[Any, float]]] = []

def clear_all_caches():
    """Clear all timed caches to prevent stale data after mutations."""
    for cache in _caches:
        cache.clear()

def timed_cache(seconds: int = 60) -> Callable:
    """Cache function results for a given number of seconds."""
    def decorator(func: Callable) -> Callable:
        cache: Dict[str, Tuple[Any, float]] = {}
        _caches.append(cache)

        @wraps(func)
        def wrapper(*args, **kwargs):
            key = str(args) + str(kwargs)
            now = time.time()
            
            if key in cache:
                result, timestamp = cache[key]
                if now - timestamp < seconds:
                    return result
            
            result = func(*args, **kwargs)
            cache[key] = (result, now)
            return result
            
        return wrapper
    return decorator
