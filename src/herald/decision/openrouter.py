from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from herald.decision.openai import OpenAICompatibleProvider
from herald.decision.port import Decider, DecisionError, ModelCatalog, ModelInfo


def parse_catalog(payload: dict) -> list[ModelInfo]:
    """Parse an OpenRouter ``/models`` payload into vendor-neutral :class:`ModelInfo`.

    This is the only place that knows OpenRouter's response shape; everything downstream sees
    :class:`ModelInfo`. Prices are per-token decimal strings; treat the parse defensively.
    """
    models: list[ModelInfo] = []
    for item in payload.get("data", []):
        if not isinstance(item, dict) or "id" not in item:
            continue
        pricing = item.get("pricing", {}) or {}
        architecture = item.get("architecture", {}) or {}
        supported = item.get("supported_parameters", []) or []
        models.append(
            ModelInfo(
                id=str(item["id"]),
                context_length=int(item.get("context_length") or 0),
                prompt_price=_as_price(pricing.get("prompt")),
                completion_price=_as_price(pricing.get("completion")),
                supports_json=("response_format" in supported or "structured_outputs" in supported),
                output_modalities=tuple(architecture.get("output_modalities") or ("text",)),
            )
        )
    return models


def _as_price(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


@dataclass(slots=True)
class OpenRouterCatalog:
    """A :class:`ModelCatalog` backed by OpenRouter's ``GET /models`` (ADR 0004).

    One implementation of the catalog port. The selection policy lives in the core
    (:mod:`herald.decision.selection`), so swapping to a local catalog changes nothing else.
    """

    api_key: str | None = None
    base_url: str = "https://openrouter.ai/api/v1"
    timeout: float = 30.0

    def list_models(self) -> list[ModelInfo]:
        request = urllib.request.Request(f"{self.base_url.rstrip('/')}/models")
        if self.api_key:
            request.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise DecisionError(f"catalog fetch failed: {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise DecisionError(f"catalog unreachable: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise DecisionError("catalog returned non-JSON") from exc
        return parse_catalog(payload)


@dataclass(slots=True)
class OpenRouterProvider:
    """An OpenRouter implementation of the decider: a catalog plus a provider for a model.

    OpenRouter speaks the OpenAI chat format, so the decider is the generic
    :class:`~herald.decision.openai.OpenAICompatibleDecider`; this adapter only supplies the
    connection facts and the recommended headers.
    """

    api_key: str
    base_url: str = "https://openrouter.ai/api/v1"
    timeout: float = 60.0
    referer: str | None = None
    title: str = "Herald"

    def catalog(self) -> ModelCatalog:
        return OpenRouterCatalog(api_key=self.api_key, base_url=self.base_url)

    def decider_for(self, model: ModelInfo) -> Decider:
        provider = OpenAICompatibleProvider(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            extra_headers=self._extra_headers(),
        )
        return provider.decider_for(model)

    def _extra_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.referer:
            headers["HTTP-Referer"] = self.referer
            headers["X-Title"] = self.title
        return headers


__all__ = ["OpenRouterCatalog", "OpenRouterProvider", "parse_catalog"]
