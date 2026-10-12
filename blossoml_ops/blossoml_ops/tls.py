"""TLS contexts.

The runtime is a conda environment moved into place with conda-pack. OpenSSL
inside it keeps the CA bundle path of the build location, which does not exist
on the server, so the bundle that ships in the environment is named explicitly.
"""

from __future__ import annotations

import ssl
import sys
from pathlib import Path

ENV_CA_BUNDLE = Path(sys.prefix) / "ssl" / "cacert.pem"


def client_context() -> ssl.SSLContext:
    cafile = str(ENV_CA_BUNDLE) if ENV_CA_BUNDLE.is_file() else None
    return ssl.create_default_context(cafile=cafile)
