from contextlib import contextmanager
from pathlib import Path
import tempfile
from unittest.mock import patch

import connections


@contextmanager
def document_connection(root):
    with tempfile.TemporaryDirectory(prefix='forkrail-document-settings-') as directory:
        settings=connections.Connections(Path(directory)/'state',Path(directory))
        settings.save('documents',{'enabled':True,'root':str(root)})
        with patch.object(connections,'STORE',settings):
            yield settings
