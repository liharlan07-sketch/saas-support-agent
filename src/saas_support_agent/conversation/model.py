"""显式启用的 HTTP JSON 模型协议，不绑定厂商 SDK。"""
from dataclasses import dataclass, field
import json
import os
from urllib.parse import urlsplit


class ModelUnavailable(Exception):
    """不向调用方泄露上游响应、地址或凭证。"""


class InvalidModelOutput(Exception):
    pass


def decode_object(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError('non-finite number')
    try:
        if not isinstance(text, str) or len(text) > 32000:
            raise ValueError('invalid content')
        result = json.loads(text, object_pairs_hook=unique, parse_constant=invalid_constant)
        if type(result) is not dict:
            raise ValueError('object required')
        return result
    except (ValueError, TypeError, RecursionError) as error:
        raise InvalidModelOutput('模型必须返回唯一键的有限 JSON 对象') from None


@dataclass(frozen=True)
class ModelConfig:
    endpoint: str
    model: str
    api_key: str = field(repr=False)

    def __post_init__(self):
        url = urlsplit(self.endpoint)
        if (url.scheme != 'https' or not url.hostname or url.username or url.password
                or url.query or url.fragment or not url.path or not self.model.strip()
                or not self.api_key.strip()):
            raise ValueError('模型配置须包含 HTTPS 完整请求地址、模型和凭证；不允许 URL 内含凭证')


class HttpJsonModel:
    """固定 messages → choices[0].message.content 协议；不执行 tool_calls。

    只有运维显式启用才创建客户端；禁止重定向和自动重试，避免凭证被转发
    或超时后重复计费。endpoint 来自服务端配置，不接受用户或模型指定。
    """
    def __init__(self, config, *, transport=None):
        import httpx
        self.config = config
        self._client = httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0),
                                    follow_redirects=False, trust_env=False, transport=transport)

    def close(self):
        self._client.close()

    def complete(self, system, payload):
        import httpx
        body = {'model': self.config.model, 'temperature': 0, 'max_tokens': 1200,
                'response_format': {'type': 'json_object'},
                'messages': [{'role': 'system', 'content': system},
                             {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]}
        try:
            with self._client.stream('POST', self.config.endpoint,
                                     headers={'Authorization': 'Bearer ' + self.config.api_key}, json=body) as response:
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 128000:
                        raise InvalidModelOutput('模型响应过大')
            envelope = decode_object(content.decode('utf-8'))
            choices = envelope.get('choices')
            if type(choices) is not list or len(choices) != 1 or choices[0].get('finish_reason') != 'stop':
                raise InvalidModelOutput('模型未完整返回单一结果')
            message = choices[0]['message']
            if message.get('tool_calls') or message.get('function_call') or message.get('refusal'):
                raise InvalidModelOutput('不接受模型工具调用或拒绝标记')
            value = message['content']
            if not isinstance(value, str):
                raise InvalidModelOutput('模型内容必须为文本 JSON')
            return value
        except httpx.HTTPError:
            raise ModelUnavailable('模型请求失败或超时') from None
        except (KeyError, TypeError, AttributeError, UnicodeError):
            raise InvalidModelOutput('模型响应结构不合法') from None


def configured_model(*, environ=None, transport=None):
    env = os.environ if environ is None else environ
    # 即使环境有遗留的 endpoint/key，默认也不构造 HTTP 客户端，更不会发请求。
    if env.get('SAAS_LLM_ENABLED', '0') != '1':
        return None
    return HttpJsonModel(ModelConfig(env.get('SAAS_LLM_ENDPOINT', ''), env.get('SAAS_LLM_MODEL', ''),
                                    env.get('SAAS_LLM_API_KEY', '')), transport=transport)
