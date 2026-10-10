"""
Data Schemas and Contracts for the OpsBot AI Engine Service.
Defines strongly-typed request and response structures for AI visual analysis.
"""

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional, Any, Dict, Union


class AIAnomalySeverity(str, Enum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass
class AIEvaluationRequest:
    """Input payload for AI visual threshold analysis."""
    image_path: str
    threshold_prompt: str = ""
    api_key: Optional[str] = None
    site_id: Optional[str] = None
    provider: str = "gemini"
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AIEvaluationResult:
    """Standardized result returned by all AI vision providers."""
    success: bool
    breached: bool
    reading: Optional[Union[str, float, int]] = None
    reason: str = ""
    severity: str = "NORMAL"
    provider: str = "gemini"
    model_used: Optional[str] = None
    raw_response: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Returns standard dict representation for backward compatibility."""
        return {
            "success": self.success,
            "breached": self.breached,
            "reading": self.reading,
            "reason": self.reason,
            "severity": self.severity,
            "provider": self.provider,
            "model_used": self.model_used,
            "error": self.error,
        }

    # Dict-like access support for seamless drop-in compatibility
    def __getitem__(self, item: str) -> Any:
        return self.to_dict()[item]

    def get(self, item: str, default: Any = None) -> Any:
        return self.to_dict().get(item, default)
