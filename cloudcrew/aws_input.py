"""Keep AWS request JSON out of process arguments and shell input."""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def cli_input(params):
    with tempfile.TemporaryDirectory(prefix='gbm-cloud-aws-') as directory:
        path = Path(directory) / 'request.json'
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(params, stream)
        yield 'file://' + str(path)
