"""Named exports for JSON-Schema-generated Pydantic models."""
from .contract_models import model_for

_MODEL_NAMES = (
    "SourceRef", "ContractMetadata", "ProbabilityRange", "ResearchSession", "ThesisDocument",
    "EvidenceItem", "RubricScore", "ChallengeRecord", "WorldlineNode", "WorldlineEdge",
    "RevealEvent", "Worldline", "WorldlineBundle", "ProbabilityAssessment", "AssetImpact",
    "DataLicense", "DerivativeSpec", "AssetRegistryEntry", "AssetRegistrySnapshot", "Integrity",
    "ShardDescriptor", "MarketDataManifest", "MarketDataRow", "MarketDataShard", "FactorAdmission",
    "FactorDefinition", "FactorLibraryManifest", "FactorLibraryShard", "ClientComputationAttestation",
    "Operation", "ErrorDetail", "ErrorResponse", "OperationStartedEvent", "OperationProgressEvent",
    "HeartbeatEvent", "OperationErrorEvent", "OperationCompleteEvent", "CreateSessionRequest",
    "CreateChallengeRequest", "ChallengeMessageRequest", "EvidenceSubmissionRequest",
    "ConfirmChallengeRequest", "WorldlineRequest", "ProbabilityRequest", "MarketDataManifestRequest",
    "MarketDataShardRequest", "ClientComputationAttestRequest", "CancelOperationRequest",
    "ResumeOperationRequest", "HealthResponse", "SessionOperationResponse", "ChallengeOperationResponse",
    "WorldlineBundleOperationResponse", "ProbabilityOperationResponse", "MarketDataManifestOperationResponse",
    "MarketDataShardOperationResponse", "ClientAttestationOperationResponse", "OperationResponse",
    "AssetSearchResponse", "MethodologyResponse", "AcceptanceFixtureExpected", "AcceptanceFixture",
)

globals().update({name: model_for(name) for name in _MODEL_NAMES})
__all__ = list(_MODEL_NAMES)
