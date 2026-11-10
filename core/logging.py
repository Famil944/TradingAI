"""Redact configured secrets even when a library formats an exception URL."""

import logging


class SecretFormatter(logging.Formatter):
    def __init__(self, secrets):
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")
        self._secrets = tuple(s for s in secrets if s)

    def format(self, record):
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, "[REDACTED]")
        return text
