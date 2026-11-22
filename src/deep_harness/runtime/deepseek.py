"""DeepSeek LangChain adapter that preserves the provider's private tool context."""

from langchain_core.messages import AIMessage
from langchain_deepseek import ChatDeepSeek


class DeepSeekChatModel(ChatDeepSeek):
    preserve_reasoning: bool = False

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(messages, stop=stop, **kwargs)
        if self.preserve_reasoning and payload.get("tools"):
            assistant_messages = (msg for msg in messages if isinstance(msg, AIMessage))
            for message in payload["messages"]:
                if message["role"] == "assistant":
                    source = next(assistant_messages)
                    reasoning = source.additional_kwargs.get("reasoning_content")
                    if isinstance(reasoning, str):
                        message["reasoning_content"] = reasoning
        return payload
