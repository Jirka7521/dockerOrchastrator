"""Validation for container / image names coming out of the config file."""

from __future__ import annotations

import re

from orchestrator.errors import ConfigError

# Deliberately wider than Docker's container-name grammar: the same field may
# also name an image (``registry/org/image:tag``) for the create-on-demand
# fallback. What we actually care about is rejecting values that would confuse
# the CLI or are obviously typos.
_ALLOWED = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]*$")


class ContainerName:
    """Namespace for container-name checks.

    Names are passed to :mod:`subprocess` as argument lists, never through a
    shell, so this is about catching config mistakes early rather than about
    quoting. The leading-character rule is the one that genuinely matters: a
    value starting with ``-`` would be parsed by ``docker`` as a flag.
    """

    MAX_LENGTH = 255

    @staticmethod
    def validate(value: str, field_path: str) -> str:
        name = value.strip()
        if not name:
            raise ConfigError(f"{field_path} must be a non-empty container name")
        if len(name) > ContainerName.MAX_LENGTH:
            raise ConfigError(
                f"{field_path} is longer than {ContainerName.MAX_LENGTH} characters"
            )
        if not _ALLOWED.match(name):
            raise ConfigError(
                f"{field_path} is not a valid container or image name: {name!r}. "
                "It must start with a letter or digit and may contain only "
                "letters, digits and the characters _ . : / @ -"
            )
        return name

    @staticmethod
    def is_valid(value: str) -> bool:
        try:
            ContainerName.validate(value, "name")
        except ConfigError:
            return False
        return True
