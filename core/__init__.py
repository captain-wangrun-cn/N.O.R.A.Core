"""core package exports."""

__all__ = ["NoraController"]


def __getattr__(name):
    if name == "NoraController":
        from core.controller import NoraController

        return NoraController
    raise AttributeError(name)
