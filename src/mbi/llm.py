"""Camada de LLM: uma interface mínima e dois adaptadores (Gemini real e roteirizado para testes).

O agente nunca importa o SDK do Gemini diretamente. Isso mantém o loop de orquestração
testável sem rede e permite trocar de provedor implementando a mesma interface.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol


class QuotaExceeded(RuntimeError):
    """Cota do provedor esgotada por um período longo (ex.: limite diário do tier gratuito)."""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    seconds: float = 0.0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.calls += other.calls
        self.seconds += other.seconds


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass
class LLMResponse:
    text: str | None
    tool_calls: list[ToolCall]
    usage: Usage
    raw: Any = None  # conteúdo nativo do provedor, reenviado no próximo turno


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


@dataclass
class Message:
    role: str  # "user" | "assistant" | "tool"
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[tuple[str, dict]] = field(default_factory=list)  # (nome, resultado)
    raw: Any = None


class LLM(Protocol):
    model: str

    def chat(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse: ...

    def json(self, system: str, prompt: str, schema: dict[str, Any]) -> tuple[dict[str, Any], Usage]: ...


class GeminiLLM:
    """Adaptador para a API do Gemini (SDK google-genai)."""

    def __init__(self, api_key: str, model: str, temperature: float = 0.2, max_retries: int = 4):
        from google import genai

        self._genai = genai
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.temperature = temperature
        self.max_retries = max_retries

    def _call(self, contents, config):
        from google.genai import errors

        delay = 2.0
        for attempt in range(self.max_retries + 1):
            try:
                t0 = time.perf_counter()
                resp = self.client.models.generate_content(model=self.model, contents=contents, config=config)
                return resp, time.perf_counter() - t0
            except errors.APIError as e:
                code = getattr(e, "code", 0)
                if code == 429:
                    wait = _retry_delay(e)
                    if "PerDay" in str(e) or wait > 90:
                        raise QuotaExceeded(
                            f"Cota do modelo {self.model} esgotada (limite diário do plano gratuito). "
                            f"Tente outro modelo com GEMINI_MODEL=... ou aguarde ~{wait / 3600:.1f}h.") from e
                    delay = max(delay, wait)
                elif code not in (500, 502, 503, 504):
                    raise
                if attempt == self.max_retries:
                    raise
                time.sleep(delay)
                delay *= 2
        raise RuntimeError("unreachable")

    @staticmethod
    def _usage(resp, seconds: float) -> Usage:
        um = getattr(resp, "usage_metadata", None)
        return Usage(
            input_tokens=(getattr(um, "prompt_token_count", 0) or 0),
            output_tokens=(getattr(um, "candidates_token_count", 0) or 0)
            + (getattr(um, "thoughts_token_count", 0) or 0),
            calls=1,
            seconds=seconds,
        )

    def _contents(self, messages: list[Message]):
        from google.genai import types

        out = []
        for m in messages:
            if m.role == "user":
                out.append(types.Content(role="user", parts=[types.Part(text=m.text or "")]))
            elif m.role == "assistant":
                # Reenvia o conteúdo nativo: modelos com "thinking" exigem as thought signatures.
                if m.raw is not None:
                    out.append(m.raw)
                else:
                    parts = [types.Part(text=m.text)] if m.text else []
                    parts += [types.Part(function_call=types.FunctionCall(name=c.name, args=c.args))
                              for c in m.tool_calls]
                    out.append(types.Content(role="model", parts=parts))
            elif m.role == "tool":
                out.append(types.Content(role="user", parts=[
                    types.Part.from_function_response(name=n, response=r) for n, r in m.tool_results]))
        return out

    def chat(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=self.temperature,
            tools=[types.Tool(function_declarations=[
                types.FunctionDeclaration(name=t.name, description=t.description,
                                          parameters_json_schema=t.parameters) for t in tools])],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        resp, secs = self._call(self._contents(messages), config)
        calls = [ToolCall(name=fc.name, args=dict(fc.args or {})) for fc in (resp.function_calls or [])]
        text = None
        if not calls:
            try:
                text = resp.text
            except (ValueError, AttributeError):
                text = None
        raw = resp.candidates[0].content if resp.candidates else None
        return LLMResponse(text=text, tool_calls=calls, usage=self._usage(resp, secs), raw=raw)

    def json(self, system: str, prompt: str, schema: dict[str, Any]) -> tuple[dict[str, Any], Usage]:
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.0,
            response_mime_type="application/json",
            response_json_schema=schema,
        )
        resp, secs = self._call(prompt, config)
        return json.loads(resp.text), self._usage(resp, secs)


def _retry_delay(e) -> float:
    m = re.search(r"retry in ([\d.]+)s|'retryDelay': '(\d+)s'", str(e))
    if not m:
        return 10.0
    return float(m.group(1) or m.group(2))


class ScriptedLLM:
    """LLM falso para testes: devolve respostas pré-definidas em ordem.

    `chat_script` é uma lista de LLMResponse; `json_fn(prompt, schema)` gera saídas estruturadas.
    """

    model = "scripted"

    def __init__(self, chat_script: list[LLMResponse] | None = None, json_fn=None):
        self.chat_script = list(chat_script or [])
        self.json_fn = json_fn
        self.chat_inputs: list[list[Message]] = []

    def chat(self, system, messages, tools):
        self.chat_inputs.append(list(messages))
        if not self.chat_script:
            return LLMResponse(text="(fim do roteiro)", tool_calls=[], usage=Usage(calls=1))
        return self.chat_script.pop(0)

    def json(self, system, prompt, schema):
        return self.json_fn(prompt, schema), Usage(calls=1)
