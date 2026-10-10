"""
Google Gemini Vision AI Provider Implementation.
Executes visual threshold evaluation via Google's Gemini REST API with
multi-model fallback cascade (gemini-2.5-flash -> gemini-1.5-flash -> gemini-2.0-flash).
"""

import json
import requests
from typing import List, Optional

from core.config import GEMINI_API_KEY
from core.logger import bot_log
from ai_engine.schemas import AIEvaluationRequest, AIEvaluationResult
from ai_engine.prompt_templates import build_threshold_system_instruction
from ai_engine.providers.base_provider import BaseVisionAIProvider


DEFAULT_MODELS: List[str] = [
    "gemini-3.8-flash",
    "gemini-3.8-pro",
    "gemini-3-flash",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash"
]


class GeminiVisionProvider(BaseVisionAIProvider):
    """Google Gemini Vision implementation of BaseVisionAIProvider."""

    def __init__(self, candidate_models: Optional[List[str]] = None):
        self.candidate_models = candidate_models or list(DEFAULT_MODELS)
        self._discovered_models: Optional[List[str]] = None

    @property
    def name(self) -> str:
        return "gemini"

    def is_configured(self, api_key: Optional[str] = None) -> bool:
        effective_key = (api_key or GEMINI_API_KEY or "").strip()
        return bool(effective_key)

    def discover_models(self, api_key: str) -> List[str]:
        """
        Dynamically queries Google ModelService to discover active, supported models.
        Picks top flash models first for optimal speed and visual inspection accuracy.
        """
        if self._discovered_models:
            return self._discovered_models

        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                found = []
                for m in data.get("models", []):
                    m_name = m.get("name", "").replace("models/", "").strip()
                    methods = m.get("supportedGenerationMethods", [])
                    if "generateContent" in methods:
                        found.append(m_name)
                if found:
                    def _rank(name: str):
                        n = name.lower()
                        if "3.8-flash" in n: return 0
                        if "flash" in n and "3" in n: return 1
                        if "flash" in n: return 2
                        if "3.8" in n: return 3
                        if "pro" in n: return 4
                        return 5
                    found.sort(key=_rank)
                    self._discovered_models = found
                    bot_log(f"[Vision Engine] Discovered {len(found)} active models from API (primary: {found[0]})")
                    return found
        except Exception as e:
            bot_log(f"[Vision Engine] Dynamic model discovery note: {e}")
        return self.candidate_models

    def evaluate_screenshot(self, request: AIEvaluationRequest) -> AIEvaluationResult:
        effective_key = (request.api_key or GEMINI_API_KEY or "").strip()
        if not effective_key:
            return AIEvaluationResult(
                success=False,
                breached=False,
                reason="Vision API key is not configured.",
                severity="NORMAL",
                provider=self.name,
                error="Missing GEMINI_API_KEY"
            )

        prompt_rule = (request.threshold_prompt or "").strip()
        system_instructions = build_threshold_system_instruction(prompt_rule)

        try:
            b64_data, mime_type = self.optimize_image(request.image_path)
        except Exception as e:
            bot_log(f"[Vision Engine] Image preparation failed: {e}", site_id=request.site_id)
            return AIEvaluationResult(
                success=False,
                breached=False,
                reason=f"Failed to prepare screenshot: {e}",
                severity="NORMAL",
                provider=self.name,
                error=str(e)
            )

        request_payload = {
            "contents": [
                {
                    "parts": [
                        {"text": system_instructions},
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": b64_data
                            }
                        }
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "response_mime_type": "application/json"
            }
        }

        # Resolve models to attempt: discovered models first, then candidate fallbacks
        models_to_try: List[str] = []
        active_discovered = self.discover_models(effective_key)
        for m in (active_discovered or []):
            if m not in models_to_try:
                models_to_try.append(m)
        for m in self.candidate_models:
            if m not in models_to_try:
                models_to_try.append(m)

        last_error = None
        for raw_model in models_to_try:
            model_name = raw_model.replace("models/", "").strip()
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={effective_key}"
            try:
                bot_log(
                    f"[Vision Engine] Evaluating snapshot with model {model_name} (rule: '{prompt_rule[:60]}...')",
                    site_id=request.site_id
                )
                resp = requests.post(url, json=request_payload, timeout=25)
                if resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if not candidates:
                        raise RuntimeError("Vision service returned empty candidate list.")

                    part = candidates[0].get("content", {}).get("parts", [{}])[0]
                    text_out = part.get("text", "").strip()

                    parsed = json.loads(text_out)
                    breached = bool(parsed.get("breached", False))
                    reading = parsed.get("reading")
                    reason = str(parsed.get("reason", "Analysis complete.")).strip()
                    severity = str(parsed.get("severity", "CRITICAL" if breached else "NORMAL")).upper()

                    bot_log(
                        f"[Vision Engine] Model {model_name} result: Breached={breached} | "
                        f"Severity={severity} | Reading={reading} | Reason={reason}",
                        site_id=request.site_id
                    )

                    return AIEvaluationResult(
                        success=True,
                        breached=breached,
                        reading=reading,
                        reason=reason,
                        severity=severity,
                        provider=self.name,
                        model_used=model_name,
                        raw_response=parsed,
                        error=None
                    )
                else:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    bot_log(
                        f"[Vision Engine] Model {model_name} returned status {resp.status_code}",
                        site_id=request.site_id
                    )
                    continue
            except Exception as exc:
                last_error = str(exc)
                bot_log(
                    f"[Vision Engine] Error with model {model_name}: {exc}",
                    site_id=request.site_id
                )
                continue

        return AIEvaluationResult(
            success=False,
            breached=False,
            reason=f"Threshold analysis could not be completed ({last_error}).",
            severity="NORMAL",
            provider=self.name,
            error=last_error
        )
