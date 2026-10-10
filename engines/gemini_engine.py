"""
Backward Compatibility Facade for Gemini Vision Engine.
Delegates directly to the dedicated, modular ai_engine service.
"""

from ai_engine import get_ai_service, evaluate_screenshot as ai_eval
from ai_engine.providers.gemini_provider import DEFAULT_MODELS as GEMINI_MODELS


def analyze_screenshot_with_gemini(image_path, threshold_prompt, api_key=None, site_id=None):
    """
    Evaluates a dashboard screenshot against a user's natural language threshold prompt.
    Delegates to the dedicated OpsBot AI Engine Service.
    """
    res = ai_eval(
        image_path=image_path,
        threshold_prompt=threshold_prompt,
        api_key=api_key,
        site_id=site_id,
        provider_name="gemini"
    )
    return res.to_dict()


__all__ = ["analyze_screenshot_with_gemini", "GEMINI_MODELS"]
