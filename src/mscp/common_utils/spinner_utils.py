# mscp/common_utils/spinner_utils.py

import functools
from yaspin import yaspin
from . import logging_config


def conditional_inject_spinner(**spinner_kwargs):
    """
    Decorator to mimic the behavior of the yaspin @inject_spinner, but suppress the
    spinner if there is any logging is enabled.
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            suppress = logging_config.verbose_logging

            sp = yaspin(**spinner_kwargs)
            if not suppress:
                sp.start()
            try:
                return func(sp, *args, **kwargs)
            finally:
                if not suppress:
                    sp.stop()

        return wrapper

    return decorator
