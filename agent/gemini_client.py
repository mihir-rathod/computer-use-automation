"""Thin wrapper around the google-genai SDK -- the discovery loop's single point of contact
with the model. Handles retry/backoff: the free-tier API returns 503 "high demand" fairly
often in practice, and that's a transient condition worth retrying, not a hard failure.
"""
from __future__ import annotations

import os
import time

from google import genai
from google.genai import types

# An alias, not a pinned dated version -- avoids breaking when a specific model gets
# deprecated for new API keys. Hit exactly this during development: "gemini-2.5-flash" 404'd
# with "This model is no longer available to new users." Using the *lite* alias specifically,
# not "gemini-flash-latest": that one resolved to a preview model with a 20-requests/day free
# quota, exhausted mid-development: lite tiers carry a materially higher free-tier quota.
DEFAULT_MODEL = "gemini-flash-lite-latest"
MAX_RETRIES = 5
BASE_BACKOFF_SECONDS = 2.0


class GeminiClient:
    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL):
        self.client = genai.Client(api_key=api_key or os.environ["GEMINI_API_KEY"])
        self.model = model

    def generate(
        self,
        contents: list[types.Content],
        tools: list[types.Tool],
        system_instruction: str | None = None,
        tool_choice: str = "ANY",
    ) -> types.GenerateContentResponse:
        """`tool_choice` defaults to "ANY" (always call a tool) for the discovery loop's own
        needs -- every turn there must be some action or terminal call, never plain prose. The
        the chat (api/chat_v1.py) is the other caller of this method and needs the opposite:
        "AUTO", so the model can answer in plain text when nothing in the catalog actually
        matches. Found via a real bug: with mode="ANY" forced unconditionally, an out-of-scope
        chatbot message ("what's the weather like today") still got force-mapped onto the
        nearest-sounding capability instead of getting a plain "that's out of scope" reply.
        """
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                return self.client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        tools=tools,
                        system_instruction=system_instruction,
                        tool_config=types.ToolConfig(
                            function_calling_config=types.FunctionCallingConfig(mode=tool_choice)
                        ),
                    ),
                )
            except Exception as exc:  # transient overload / rate limit / network blip
                last_exc = exc
                if attempt < MAX_RETRIES - 1:
                    time.sleep(BASE_BACKOFF_SECONDS * (2 ** attempt))
        raise RuntimeError(f"Gemini call failed after {MAX_RETRIES} attempts: {last_exc}") from last_exc
