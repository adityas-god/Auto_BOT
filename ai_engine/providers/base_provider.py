"""
Base Vision AI Provider Contract.
Defines the abstract interface and common image manipulation utilities
for all concrete vision AI providers (Gemini, OpenAI, Claude, Local VLM).
"""

import os
import io
import base64
from abc import ABC, abstractmethod
from typing import Tuple

from ai_engine.schemas import AIEvaluationRequest, AIEvaluationResult


class BaseVisionAIProvider(ABC):
    """Abstract interface that all vision AI providers must implement."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique provider identifier (e.g. 'gemini', 'openai')."""
        pass

    @abstractmethod
    def is_configured(self, api_key: str = None) -> bool:
        """Returns True if the provider has the necessary credentials to execute requests."""
        pass

    @abstractmethod
    def evaluate_screenshot(self, request: AIEvaluationRequest) -> AIEvaluationResult:
        """
        Evaluates a screenshot image against the user's natural language threshold prompt.
        Must return a standardized AIEvaluationResult.
        """
        pass

    @staticmethod
    def optimize_image(image_path: str, max_dim: int = 1920) -> Tuple[str, str]:
        """
        Downscales and optimizes image to JPEG for low payload latency and fast transmission.
        Returns: (base64_encoded_str, mime_type)
        """
        if not image_path or not os.path.exists(image_path):
            raise FileNotFoundError(f"Image file not found: {image_path}")

        try:
            from PIL import Image
            with Image.open(image_path) as img:
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                w, h = img.size
                if max(w, h) > max_dim:
                    scale = max_dim / float(max(w, h))
                    new_w = int(w * scale)
                    new_h = int(h * scale)
                    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

                buffer = io.BytesIO()
                img.save(buffer, format="JPEG", quality=85, optimize=True)
                encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")
                return encoded, "image/jpeg"
        except Exception:
            with open(image_path, "rb") as f:
                raw_bytes = f.read()
                encoded = base64.b64encode(raw_bytes).decode("utf-8")
                ext = os.path.splitext(image_path)[1].lower()
                mime = "image/png" if ext == ".png" else "image/jpeg"
                return encoded, mime
