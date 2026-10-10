"""
OpenAI Vision Provider Implementation (Extensibility & Scaling).
Provides support for GPT-4o / GPT-4o-mini vision models when configured.
"""

import os
import json
import requests
from typing import Optional

from core.logger import bot_log
from ai_engine.schemas import AIEvaluationRequest, AIEvaluationResult
from ai_engine.prompt_templates import build_threshold_system_instruction
from ai_engine.providers.base_provider import BaseVisionAIProvider


class OpenAIVisionProvider(BaseVisionAIProvider):
    """OpenAI Vision implementation for GPT-4o."""

    def __init__(self, model_name: str = "gpt-4o-mini"):
        self.model_name = model_name

    @property
    def name(self) -> str:
        return "openai"

    def is_configured(self, api_key: Optional[str] = None) -> bool:
        effective_key = (api_key or os.getenv("OPENAI_API_KEY", "")).strip()
        return bool(effective_key)

    def evaluate_screenshot(self, request: AIEvaluationRequest) -> AIEvaluationResult:
        effective_key = (request.api_key or os.getenv("OPENAI_API_KEY", "")).strip()
        if not effective_key:
            return AIEvaluationResult(
                success=False,
                breached=False,
                reason="OpenAI API key is not configured.",
                severity="NORMAL",
                provider=self.name,
                error="Missing OPENAI_API_KEY"
            )

        prompt_rule = (request.threshold_prompt or "").strip()
        system_instructions = build_threshold_system_instruction(prompt_rule)

        try:
            b64_data, mime_type = self.optimize_image(request.image_path)
        except Exception as e:
            return AIEvaluationResult(
                success=False,
                breached=False,
                reason=f"Failed to prepare screenshot: {e}",
                severity="NORMAL",
                provider=self.name,
                error=str(e)
            )

        headers = {
            "Authorization": f"Bearer {effective_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": self.model_name,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_instructions},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Evaluate the visual threshold criteria for this dashboard snapshot."},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{b64_data}"
                            }
                        }
                    ]
                }
            ],
            "max_tokens": 300,
            "temperature": 0.1
        }

        try:
            bot_log(f"[AI Engine][OpenAI] Sending screenshot to {self.model_name}...", site_id=request.site_id)
            resp = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload, timeout=25)
            if resp.status_code == 200:
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                parsed = json.loads(content)
                breached = bool(parsed.get("breached", False))
                reading = parsed.get("reading")
                reason = str(parsed.get("reason", "Analysis complete.")).strip()
                severity = str(parsed.get("severity", "CRITICAL" if breached else "NORMAL")).upper()

                return AIEvaluationResult(
                    success=True,
                    breached=breached,
                    reading=reading,
                    reason=reason,
                    severity=severity,
                    provider=self.name,
                    model_used=self.model_name,
                    raw_response=parsed,
                    error=None
                )
            else:
                return AIEvaluationResult(
                    success=False,
                    breached=False,
                    reason=f"OpenAI API error ({resp.status_code}).",
                    severity="NORMAL",
                    provider=self.name,
                    error=resp.text[:200]
                )
        except Exception as exc:
            return AIEvaluationResult(
                success=False,
                breached=False,
                reason=f"Failed to communicate with OpenAI: {exc}",
                severity="NORMAL",
                provider=self.name,
                error=str(exc)
            )
