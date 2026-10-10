"""
OpsBot AI Engine Service Orchestrator.
Central service that coordinates vision analysis requests across pluggable providers.
"""

import threading
from typing import Optional, Dict, Any, List

from core.logger import bot_log
from core.config import GEMINI_API_KEY
from ai_engine.schemas import AIEvaluationRequest, AIEvaluationResult
from ai_engine.providers import get_provider, list_providers, BaseVisionAIProvider


class AIEngineService:
    """
    High-level AI Service coordinating visual evaluation, provider routing,
    and telemetry diagnostics for OpsBot.
    """

    def __init__(self, default_provider: str = "gemini"):
        self.default_provider = default_provider
        self._lock = threading.RLock()

    def evaluate_screenshot(
        self,
        image_path: str,
        threshold_prompt: str = "",
        api_key: Optional[str] = None,
        site_id: Optional[str] = None,
        provider_name: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None
    ) -> AIEvaluationResult:
        """
        Main entry point for evaluating a screenshot against natural language criteria.
        Routes to the requested or default provider.
        """
        target_provider_name = provider_name or self.default_provider
        provider = get_provider(target_provider_name)

        if not provider:
            # Fallback to default provider if specified one is not registered
            provider = get_provider(self.default_provider)
            if not provider:
                return AIEvaluationResult(
                    success=False,
                    breached=False,
                    reason=f"No AI provider available (requested: '{target_provider_name}').",
                    severity="NORMAL",
                    provider=target_provider_name,
                    error=f"Provider '{target_provider_name}' not registered"
                )

        request = AIEvaluationRequest(
            image_path=image_path,
            threshold_prompt=threshold_prompt,
            api_key=api_key,
            site_id=site_id,
            provider=provider.name,
            options=options or {}
        )

        try:
            return provider.evaluate_screenshot(request)
        except Exception as e:
            bot_log(f"[AI Engine] Unexpected error in {provider.name}: {e}", site_id=site_id)
            return AIEvaluationResult(
                success=False,
                breached=False,
                reason=f"AI engine execution exception: {e}",
                severity="NORMAL",
                provider=provider.name,
                error=str(e)
            )

    def get_service_status(self) -> Dict[str, Any]:
        """Returns diagnostic health information about available AI providers."""
        providers = list_providers()
        gemini_prov = get_provider("gemini")
        return {
            "status": "active",
            "default_provider": self.default_provider,
            "registered_providers": providers,
            "gemini_configured": gemini_prov.is_configured() if gemini_prov else False,
            "candidate_models": getattr(gemini_prov, "candidate_models", []) if gemini_prov else []
        }


# Global thread-safe singleton
_AI_SERVICE_INSTANCE: Optional[AIEngineService] = None
_SERVICE_LOCK = threading.Lock()


def get_ai_service() -> AIEngineService:
    """Retrieves or initializes the global AIEngineService singleton."""
    global _AI_SERVICE_INSTANCE
    if _AI_SERVICE_INSTANCE is None:
        with _SERVICE_LOCK:
            if _AI_SERVICE_INSTANCE is None:
                _AI_SERVICE_INSTANCE = AIEngineService()
    return _AI_SERVICE_INSTANCE
