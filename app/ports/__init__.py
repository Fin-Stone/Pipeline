"""Protocol definitions — the decoupling seams.

Pipeline code imports from this package and never from a concrete
implementation. Swapping Postgres for MySQL, local disk for S3, or a log
notifier for ntfy is a change of one factory line and nothing else.

See the development rules in README.md, Rule 1.
"""

from .blobstore import BlobStore
from .notifier import Notifier
from .parser import AdapterMatch, ParseError, StatementAdapter
from .repository import (
    DocumentRecord,
    LedgerRepository,
    StatusCounts,
    TxnRecord,
)
from .source import DiscoveredFile, DocumentSource

__all__ = [
    "AdapterMatch",
    "BlobStore",
    "DiscoveredFile",
    "DocumentRecord",
    "DocumentSource",
    "LedgerRepository",
    "Notifier",
    "ParseError",
    "StatementAdapter",
    "StatusCounts",
    "TxnRecord",
]
