from typing import Any, AsyncIterator, Callable, Optional, Sequence

import litellm
from langchain_core.language_models.chat_models import BaseChatModel

# LiteLLM validates request parameters against each provider's supported set and
# raises if an unsupported one is present. Reasoning effort, for example, is only
# meaningful for o-series / Anthropic-thinking models, yet the harness sends it
# unconditionally — so let LiteLLM drop per-provider-unsupported parameters
# silently rather than fail the call. This is what makes one model configuration
# portable across providers.
litellm.drop_params = True
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    ToolMessage,
)
from langchain_core.messages.tool import tool_call as create_tool_call
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import SecretStr


class ChatLiteLLMModel(BaseChatModel):
    """A LangChain ``BaseChatModel`` backed by LiteLLM, the single route to every
    provider (Anthropic, OpenAI, Gemini, Bedrock, the OpenAI-compatible family, and
    any custom OpenAI-compatible server). LiteLLM owns each provider's auth, base
    URL, request format, streaming wire format, and reasoning-token normalization;
    this class only translates between LangChain messages and LiteLLM's
    OpenAI-shaped request/response, so the harness's existing tool-binding, chunk
    merging, and reasoning round-trip keep working unchanged across providers.

    One adapter replaces the former ``ChatOpenAI`` + ``ReasoningChatOpenAI`` pair:
    there is no longer an OpenAI-only path and a separate reasoning subclass.
    LiteLLM normalizes ``reasoning_content`` (and ``thinking_blocks``) into a single
    field for every reasoning model, so the reasoning is captured on the way out and
    re-injected on the way in here, uniformly.
    """

    model: str
    api_key: Optional[SecretStr] = None
    api_base: Optional[str] = None
    temperature: float = 0.0
    reasoning_effort: Optional[str] = None
    maximum_tokens: Optional[int] = None
    timeout: Optional[float] = None
    default_headers: dict[str, str] = {}

    @property
    def _llm_type(self) -> str:
        return "litellm"

    def context_window(self) -> int:
        """The model's maximum input context in tokens, from LiteLLM's model-info
        map. Used to show how full the context is. Returns 0 when the model is not
        in the map (a custom or unknown endpoint), which callers treat as unknown."""
        try:
            info = litellm.get_model_info(self.model)
        except Exception:  # noqa: BLE001 — unknown model ids raise; treat as unknown
            return 0
        return int(info.get("max_input_tokens") or info.get("max_tokens") or 0)

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "api_base": self.api_base,
            "temperature": self.temperature,
            "reasoning_effort": self.reasoning_effort,
        }

    # Tool binding.

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: Optional[str] = None,
        parallel_tool_calls: Optional[bool] = None,
        **kwargs: Any,
    ) -> Runnable:
        formatted_tools = [convert_to_openai_tool(tool) for tool in tools]
        bound: dict[str, Any] = {"tools": formatted_tools}
        if tool_choice is not None:
            bound["tool_choice"] = tool_choice
        if parallel_tool_calls is not None:
            bound["parallel_tool_calls"] = parallel_tool_calls
        return self.bind(**bound, **kwargs)

    # Message translation between LangChain messages and LiteLLM request dicts.

    @staticmethod
    def _messages_to_dicts(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
        dicts: list[dict[str, Any]] = []
        for message in messages:
            role = ChatLiteLLMModel._role_for(message)
            entry: dict[str, Any] = {"role": role, "content": message.content}
            if isinstance(message, AIMessage):
                tool_calls = ChatLiteLLMModel._tool_calls_to_openai(message.tool_calls)
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                reasoning = message.additional_kwargs.get("reasoning_content")
                if reasoning:
                    entry["reasoning_content"] = reasoning
            elif isinstance(message, ToolMessage):
                entry["tool_call_id"] = message.tool_call_id
            dicts.append(entry)
        return dicts

    @staticmethod
    def _role_for(message: BaseMessage) -> str:
        # LangChain's message types map onto the OpenAI role names LiteLLM expects.
        name = message.__class__.__name__
        if isinstance(message, ToolMessage):
            return "tool"
        return {
            "SystemMessage": "system",
            "HumanMessage": "user",
            "AIMessage": "assistant",
            "AIMessageChunk": "assistant",
            "ToolMessage": "tool",
        }.get(name, "user")

    @staticmethod
    def _tool_calls_to_openai(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        import json as _json

        rendered: list[dict[str, Any]] = []
        for call in tool_calls:
            # LangChain ToolCall stores the parsed arguments under ``args``; the
            # OpenAI wire format we serialize back to uses ``arguments``.
            arguments = call.get("args")
            serialized = arguments if isinstance(arguments, str) else _json.dumps(arguments)
            rendered.append({
                "id": call.get("id"),
                "type": "function",
                "function": {"name": call.get("name"), "arguments": serialized},
            })
        return rendered

    # Shared kwargs assembled for every LiteLLM completion call.

    def _completion_kwargs(self, **kwargs: Any) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
        }
        if self.api_key is not None:
            unsealed = self.api_key.get_secret_value()
            if unsealed:
                params["api_key"] = unsealed
        if self.api_base:
            params["api_base"] = self.api_base
        if self.reasoning_effort:
            params["reasoning_effort"] = self.reasoning_effort
        if self.maximum_tokens is not None:
            params["max_tokens"] = self.maximum_tokens  # litellm/OpenAI API param name
        if self.timeout is not None:
            params["timeout"] = self.timeout
        if self.default_headers:
            params["extra_headers"] = self.default_headers
        # Caller-supplied kwargs (tools, tool_choice, parallel_tool_calls, stop)
        # override the model defaults so bind_tools() bindings reach LiteLLM.
        params.update({key: value for key, value in kwargs.items() if value is not None})
        return params

    # Streaming generation.

    async def _astream(
        self,
        messages: Sequence[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager=None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        # include_usage asks the provider for a trailing usage chunk so the real
        # prompt/completion token counts are reported for the streamed turn (LiteLLM
        # drops this option for providers that don't support it, and normalizes the
        # usage object across providers either way).
        params = self._completion_kwargs(
            stop=stop, stream=True, stream_options={"include_usage": True}, **kwargs,
        )
        stream = await litellm.acompletion(
            messages=self._messages_to_dicts(messages),
            **params,
        )
        async for chunk in stream:
            generation_chunk = self._litellm_chunk_to_generation_chunk(chunk)
            if generation_chunk is not None:
                yield generation_chunk

    @staticmethod
    def _usage_metadata(usage: Any) -> Optional[dict[str, Any]]:
        """Normalize a LiteLLM ``Usage`` object into a LangChain ``UsageMetadata``
        dict, so real per-call token counts ride along on the message and merge
        automatically when streamed chunks are combined. Returns ``None`` when the
        response carries no usage."""
        if usage is None:
            return None

        def _value(source: Any, key: str) -> int:
            if source is None:
                return 0
            if isinstance(source, dict):
                return int(source.get(key) or 0)
            return int(getattr(source, key, 0) or 0)

        input_tokens = _value(usage, "prompt_tokens")
        output_tokens = _value(usage, "completion_tokens")
        total_tokens = _value(usage, "total_tokens") or (input_tokens + output_tokens)
        if not (input_tokens or output_tokens or total_tokens):
            return None
        metadata: dict[str, Any] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }
        prompt_details = usage.get("prompt_tokens_details") if isinstance(usage, dict) else getattr(usage, "prompt_tokens_details", None)
        cache_read = _value(prompt_details, "cached_tokens")
        if cache_read:
            metadata["input_token_details"] = {"cache_read": cache_read}
        completion_details = usage.get("completion_tokens_details") if isinstance(usage, dict) else getattr(usage, "completion_tokens_details", None)
        reasoning = _value(completion_details, "reasoning_tokens")
        if reasoning:
            metadata["output_token_details"] = {"reasoning": reasoning}
        return metadata

    @staticmethod
    def _litellm_chunk_to_generation_chunk(chunk: Any) -> Optional[ChatGenerationChunk]:
        usage_metadata = ChatLiteLLMModel._usage_metadata(getattr(chunk, "usage", None))
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            # The trailing include_usage chunk (and some providers' final chunk)
            # carries usage but no choices — surface an empty message that only
            # transports the token counts so the merged response accumulates them.
            if usage_metadata is not None:
                return ChatGenerationChunk(message=AIMessageChunk(content="", usage_metadata=usage_metadata))
            return None
        choice = choices[0]
        delta = getattr(choice, "delta", None)
        if delta is None:
            if usage_metadata is not None:
                return ChatGenerationChunk(message=AIMessageChunk(content="", usage_metadata=usage_metadata))
            return None
        content = getattr(delta, "content", None) or ""
        tool_call_chunks: list[dict[str, Any]] = []
        for call in getattr(delta, "tool_calls", None) or []:
            function = getattr(call, "function", None)
            tool_call_chunks.append({
                "index": getattr(call, "index", 0) or 0,
                "id": getattr(call, "id", None),
                "name": getattr(function, "name", None) if function else None,
                # The OpenAI streaming delta exposes the partial JSON as
                # function.arguments; langchain-core's ToolCallChunk stores it
                # under the ``args`` key (not ``arguments``).
                "args": getattr(function, "arguments", None) if function else None,
                "type": "tool_call_chunk",
            })
        reasoning = getattr(delta, "reasoning_content", None)
        if not reasoning:
            # Some providers nest reasoning under a different attribute.
            reasoning = getattr(delta, "reasoning", None)
        additional_kwargs: dict[str, Any] = {}
        if reasoning:
            additional_kwargs["reasoning_content"] = reasoning
        message = AIMessageChunk(
            content=content,
            tool_call_chunks=tool_call_chunks,
            additional_kwargs=additional_kwargs,
            usage_metadata=usage_metadata,
        )
        finish_reason = getattr(choice, "finish_reason", None)
        generation_info = {"finish_reason": finish_reason} if finish_reason else None
        return ChatGenerationChunk(message=message, generation_info=generation_info)

    # Non-streaming generation.

    async def _agenerate(
        self,
        messages: Sequence[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager=None,
        **kwargs: Any,
    ) -> ChatResult:
        params = self._completion_kwargs(stop=stop, **kwargs)
        response = await litellm.acompletion(
            messages=self._messages_to_dicts(messages),
            **params,
        )
        return ChatLiteLLMModel._response_to_result(response)

    def _generate(
        self,
        messages: Sequence[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager=None,
        **kwargs: Any,
    ) -> ChatResult:
        params = self._completion_kwargs(stop=stop, **kwargs)
        response = litellm.completion(
            messages=self._messages_to_dicts(messages),
            **params,
        )
        return ChatLiteLLMModel._response_to_result(response)

    @staticmethod
    def _response_to_result(response: Any) -> ChatResult:
        import json as _json

        choices = getattr(response, "choices", None) or []
        if not choices:
            return ChatResult(generations=[])
        message_obj = getattr(choices[0], "message", None)
        content = getattr(message_obj, "content", None) or ""
        additional_kwargs: dict[str, Any] = {}
        reasoning = getattr(message_obj, "reasoning_content", None)
        if reasoning:
            additional_kwargs["reasoning_content"] = reasoning
        tool_calls: list[dict[str, Any]] = []
        for call in getattr(message_obj, "tool_calls", None) or []:
            function = getattr(call, "function", None)
            raw_arguments = getattr(function, "arguments", None) if function else None
            try:
                parsed_arguments = _json.loads(raw_arguments) if raw_arguments else {}
            except (TypeError, ValueError):
                parsed_arguments = raw_arguments
            tool_calls.append({
                "name": getattr(function, "name", None) if function else None,
                "args": parsed_arguments,
                "id": getattr(call, "id", None),
            })
        message = AIMessage(
            content=content,
            tool_calls=tool_calls if tool_calls else None,
            additional_kwargs=additional_kwargs,
            usage_metadata=ChatLiteLLMModel._usage_metadata(getattr(response, "usage", None)),
        )
        return ChatResult(generations=[ChatGeneration(message=message)])
