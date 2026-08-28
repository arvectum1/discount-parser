from .discovery import AutoDiscoveryProvider
from .engine import ExtractionEngine
from .html_records import SemanticHTMLRecordProvider
from .models import (
    Candidate,
    Evidence,
    ExtractionResult,
    FieldDecision,
    FieldSpec,
    FieldStatus,
    RawAsset,
)
from .protocols import CandidateProvider, RecordProvider
from .providers import AttributeProvider
from .records import (
    AttributeRecordProvider,
    JSONLDRecordProvider,
    MultiRecordExtractionEngine,
    RecordBoundary,
    RecordBoundaryStatus,
    RecordExtractionResult,
    RecordProviderResult,
    RecordSetResult,
    RecordStatus,
    make_record_id,
)

__all__ = [
    "AttributeProvider",
    "AttributeRecordProvider",
    "AutoDiscoveryProvider",
    "Candidate",
    "CandidateProvider",
    "Evidence",
    "ExtractionEngine",
    "ExtractionResult",
    "FieldDecision",
    "FieldSpec",
    "FieldStatus",
    "JSONLDRecordProvider",
    "MultiRecordExtractionEngine",
    "RawAsset",
    "RecordBoundary",
    "RecordBoundaryStatus",
    "RecordExtractionResult",
    "RecordProvider",
    "RecordProviderResult",
    "RecordSetResult",
    "RecordStatus",
    "SemanticHTMLRecordProvider",
    "make_record_id",
]
