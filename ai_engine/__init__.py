"""
OpsBot AI Engine Service.
Dedicated, extensible subsystem for visual anomaly detection and AI threshold evaluation.
"""

from ai_engine.schemas import AIEvaluationRequest, AIEvaluationResult, AIAnomalySeverity
from ai_engine.service import AIEngineService, get_ai_service
from ai_engine.prompt_templates import build_threshold_system_instruction


def evaluate_screenshot(
    image_path: str,
    threshold_prompt: str = "",
    api_key: str = None,
    site_id: str = None,
    provider_name: str = "gemini",
    options: dict = None
) -> AIEvaluationResult:
    """Convenience helper that delegates to the global AIEngineService."""
    service = get_ai_service()
    return service.evaluate_screenshot(
        image_path=image_path,
        threshold_prompt=threshold_prompt,
        api_key=api_key,
        site_id=site_id,
        provider_name=provider_name,
        options=options
    )


__all__ = [
    "AIEngineService",
    "get_ai_service",
    "AIEvaluationRequest",
    "AIEvaluationResult",
    "AIAnomalySeverity",
    "evaluate_screenshot",
    "build_threshold_system_instruction"
]
