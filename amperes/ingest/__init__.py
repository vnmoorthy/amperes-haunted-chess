"""Episode ingestion: adapter interface and offline adapters (canonical JSONL, CSV package)."""

from amperes.ingest.base import ADAPTERS, Adapter, ParsedRecord, RecordError, get_adapter  # noqa: F401
