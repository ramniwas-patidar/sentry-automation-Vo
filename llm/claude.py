import json
import logging

from openai import OpenAI

from config import settings

logger = logging.getLogger(__name__)


class ClaudeLLM:
    """LLM provider using OpenAI-compatible API (GPT / Claude via proxy)."""

    def __init__(self):
        self.client = OpenAI(api_key=settings.OPENAI_API_KEY)
        self.model = settings.OPENAI_MODEL

    def chat(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.2,
        json_mode: bool = False,
        max_tokens: int = None,
    ) -> str:
        """Send a chat completion request. Returns the raw response text."""
        logger.info("[LLM] ── REQUEST ──")
        logger.info(f"[LLM] Model: {self.model}")
        logger.info(f"[LLM] System prompt: {len(system_prompt)} chars")
        logger.info(f"[LLM] User message: {len(user_message)} chars")
        logger.info(f"[LLM] User message preview:\n{user_message[:2000]}{'...' if len(user_message) > 2000 else ''}")

        kwargs = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": temperature,
            "timeout": 120,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if max_tokens:
            kwargs["max_tokens"] = max_tokens

        response = self.client.chat.completions.create(**kwargs)

        if not response.choices:
            raise ValueError("LLM returned empty response (no choices)")

        raw = response.choices[0].message.content
        if raw is None:
            raise ValueError("LLM returned None content")

        logger.info("[LLM] ── RESPONSE ──")
        logger.info(f"[LLM] Response ({len(raw)} chars):\n{raw[:3000]}{'...' if len(raw) > 3000 else ''}")
        logger.info(f"[LLM] Usage: {response.usage}")

        return raw

    def chat_json(
        self,
        system_prompt: str,
        user_message: str,
        temperature: float = 0.2,
    ) -> dict:
        """Send a chat request and parse the response as JSON."""
        raw = self.chat(
            system_prompt=system_prompt,
            user_message=user_message,
            temperature=temperature,
            json_mode=True,
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            logger.error(f"[LLM] Failed to parse JSON response: {e}")
            logger.error(f"[LLM] Raw response: {raw[:1000]}")
            raise ValueError(f"LLM returned invalid JSON: {e}")
