"""Errors that say who has to act.

The distinction this module exists for is between a step that *failed* and a
step that was never configured to run. They look the same to the code that
catches them and they are not the same thing to anybody reading the result: a
failure is news an operator can act on — retry it, chase the service that was
down — while a misconfiguration is a deployment's own, and the operator holding
the review queue can do nothing with it but be alarmed.

So a `ConfigurationError` deliberately does **not** inherit `ValueError`. The
admin API turns a `ValueError` into a 422 carrying its message, and the message
of a configuration error names the deployment's own settings. Those belong in
the server log, where the person who can fix them is looking.
"""

from __future__ import annotations


class ConfigurationError(Exception):
    """This deployment is not configured for what was just asked of it."""
