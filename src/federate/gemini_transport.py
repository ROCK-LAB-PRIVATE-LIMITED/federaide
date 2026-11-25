"""
Native Gemini v1beta REST/SSE Transport Translator for ChatOpenAI.
Routes generativelanguage.googleapis.com requests to:
  - POST /v1beta/models/{model}:generateContent
  - POST /v1beta/models/{model}:streamGenerateContent?alt=sse
"""

import json
import re
import time
from typing import Any, Iterator, AsyncIterator, Optional

import httpx
import langchain_openai

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_DATA_URI_RE = re.compile(r"^data:([^;]+);base64,(.+)$", re.DOTALL)


def _is_gemini_url(url: str) -> bool:
    low = (url or "").lower()
    return "generativelanguage.googleapis.com" in low and "/openai" not in low


def _normalize_gemini_base(url: str) -> str:
    clean = (url or "").strip().rstrip("/")
    for suffix in ("/openai", "/v1beta/openai"):
        if clean.lower().endswith("/openai"):
            clean = clean[:-len("/openai")].rstrip("/")
    if not clean.lower().endswith("/v1beta"):
        if "/v1beta" in clean.lower():
            idx = clean.lower().index("/v1beta")
            clean = clean[: idx + len("/v1beta")]
        else:
            clean = GEMINI_BASE_URL
    return clean


def _extract_api_key(request: httpx.Request) -> str:
    x_key = request.headers.get("x-goog-api-key")
    if x_key:
        return x_key.strip()
    auth = request.headers.get("authorization", "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return auth


def _extract_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text", "")))
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(p for p in parts if p)
    return str(content) if content is not None else ""


def _convert_user_parts(content: Any) -> list:
    parts = []
    if isinstance(content, str):
        if content:
            parts.append({"text": content})
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                if block:
                    parts.append({"text": str(block)})
                continue
            btype = block.get("type")
            if btype == "text":
                txt = block.get("text", "")
                if txt:
                    parts.append({"text": txt})
            elif btype == "image_url":
                url = (block.get("image_url") or {}).get("url", "").strip()
                if not url:
                    continue
                m = _DATA_URI_RE.match(url)
                if m:
                    parts.append({
                        "inlineData": {
                            "mimeType": m.group(1).strip(),
                            "data": m.group(2).strip(),
                        }
                    })
                else:
                    parts.append({"fileData": {"fileUri": url}})
    if not parts:
        parts.append({"text": " "})
    return parts


def _translate_openai_to_gemini(request: httpx.Request) -> tuple[httpx.Request, bool, str]:
    body = json.loads(request.content.decode("utf-8")) if request.content else {}
    model = str(body.get("model") or "gemini-2.5-flash").strip()
    if model.startswith("models/"):
        model = model[len("models/"):]
    is_stream = bool(body.get("stream", False))

    base = _normalize_gemini_base(str(request.url).split("/chat/completions")[0])
    action = "streamGenerateContent?alt=sse" if is_stream else "generateContent"
    target_url = f"{base}/models/{model}:{action}"

    api_key = _extract_api_key(request)
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if is_stream else "application/json",
    }
    if api_key:
        headers["x-goog-api-key"] = api_key

    sys_texts = []
    contents = []
    tool_call_id_to_name = {}

    for msg in body.get("messages", []):
        role = msg.get("role")
        content = msg.get("content", "")

        if role in ("system", "developer"):
            txt = _extract_text_from_content(content).strip()
            if txt:
                sys_texts.append(txt)
            continue

        if role == "user":
            g_role = "user"
            g_parts = _convert_user_parts(content)

        elif role == "assistant":
            g_role = "model"
            g_parts = []
            txt = _extract_text_from_content(content)
            if txt:
                g_parts.append({"text": txt})

            tool_calls = msg.get("tool_calls") or []
            turn_has_real_sig = False
            for idx, tc in enumerate(tool_calls):
                func = tc.get("function") or {}
                fn_name = func.get("name") or "unknown_tool"
                tc_id = tc.get("id")
                if tc_id:
                    tool_call_id_to_name[tc_id] = fn_name

                raw_args = func.get("arguments") or "{}"
                if isinstance(raw_args, str):
                    try:
                        args_obj = json.loads(raw_args) if raw_args.strip() else {}
                    except Exception:
                        args_obj = {}
                elif isinstance(raw_args, dict):
                    args_obj = raw_args
                else:
                    args_obj = {}
                if not isinstance(args_obj, dict):
                    args_obj = {"value": args_obj}

                fc_part: dict[str, Any] = {
                    "functionCall": {"name": fn_name, "args": args_obj}
                }
                sig = (tc.get("extra_content") or {}).get("google", {}).get("thought_signature")
                if sig and sig != "skip_thought_signature_validator":
                    fc_part["thoughtSignature"] = sig
                    turn_has_real_sig = True
                elif idx == 0 and not turn_has_real_sig:
                    fc_part["thoughtSignature"] = "skip_thought_signature_validator"
                g_parts.append(fc_part)

            if not g_parts:
                g_parts.append({"text": " "})

        elif role == "tool":
            g_role = "user"
            tc_id = msg.get("tool_call_id")
            fn_name = msg.get("name") or tool_call_id_to_name.get(tc_id) or "unknown_tool"
            if isinstance(content, dict):
                resp_obj = content
            else:
                resp_obj = {"result": _extract_text_from_content(content)}
            g_parts = [{"functionResponse": {"name": fn_name, "response": resp_obj}}]

        else:
            continue

        # Merge consecutive same-role turns; keep functionResponse parts first in user turns
        if contents and contents[-1]["role"] == g_role:
            contents[-1]["parts"].extend(g_parts)
            if g_role == "user":
                fr_parts = [p for p in contents[-1]["parts"] if "functionResponse" in p]
                other_parts = [p for p in contents[-1]["parts"] if "functionResponse" not in p]
                contents[-1]["parts"] = fr_parts + other_parts
        else:
            contents.append({"role": g_role, "parts": g_parts})

    if not contents:
        contents = [{"role": "user", "parts": [{"text": "Hello"}]}]

    gemini_body: dict[str, Any] = {"contents": contents}

    if sys_texts:
        gemini_body["systemInstruction"] = {"parts": [{"text": "\n\n".join(sys_texts)}]}

    gen_cfg: dict[str, Any] = {}
    if body.get("temperature") is not None:
        gen_cfg["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        gen_cfg["topP"] = body["top_p"]
    max_tok = body.get("max_completion_tokens") or body.get("max_tokens")
    if max_tok is not None:
        gen_cfg["maxOutputTokens"] = max_tok
    if body.get("stop"):
        stop_val = body["stop"]
        gen_cfg["stopSequences"] = [stop_val] if isinstance(stop_val, str) else list(stop_val)

    effort = body.get("reasoning_effort")
    if effort and str(effort).lower() not in ("none", ""):
        eff_low = str(effort).lower()
        if "2.5" in model:
            budget = {"minimal": 512, "low": 1024, "medium": 8192, "high": 24576}.get(eff_low, 8192)
            gen_cfg["thinkingConfig"] = {"thinkingBudget": budget}
        else:
            lvl = eff_low.upper()
            if lvl in ("MINIMAL", "LOW", "MEDIUM", "HIGH"):
                gen_cfg["thinkingConfig"] = {"thinkingLevel": lvl}

    if gen_cfg:
        gemini_body["generationConfig"] = gen_cfg

    tools = body.get("tools") or []
    if tools:
        fn_decls = []
        for t in tools:
            func = t.get("function") or {}
            decl: dict[str, Any] = {
                "name": func.get("name", "tool"),
                "description": func.get("description", ""),
            }
            params = func.get("parameters")
            if isinstance(params, dict) and (params.get("properties") or params.get("type") == "object"):
                decl["parametersJsonSchema"] = params
            fn_decls.append(decl)
        if fn_decls:
            gemini_body["tools"] = [{"functionDeclarations": fn_decls}]

        tc_opt = body.get("tool_choice")
        if tc_opt == "none":
            gemini_body["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
        elif tc_opt == "required":
            gemini_body["toolConfig"] = {"functionCallingConfig": {"mode": "ANY"}}
        elif tc_opt == "auto":
            gemini_body["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
        elif isinstance(tc_opt, dict) and tc_opt.get("type") == "function":
            fn_n = (tc_opt.get("function") or {}).get("name")
            if fn_n:
                gemini_body["toolConfig"] = {
                    "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [fn_n]}
                }

    new_req = httpx.Request("POST", target_url, headers=headers, content=json.dumps(gemini_body).encode("utf-8"))
    return new_req, is_stream, model


def _map_finish_reason(reason: Optional[str], has_tools: bool) -> str:
    if has_tools:
        return "tool_calls"
    if not reason:
        return "stop"
    r = reason.upper()
    if r == "MAX_TOKENS":
        return "length"
    if r in ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"):
        return "content_filter"
    return "stop"


def _map_usage(usage_meta: Optional[dict]) -> Optional[dict]:
    if not isinstance(usage_meta, dict):
        return None
    prompt_t = int(usage_meta.get("promptTokenCount", 0) or 0)
    comp_t = int(usage_meta.get("candidatesTokenCount", 0) or 0)
    total_t = int(usage_meta.get("totalTokenCount", prompt_t + comp_t) or (prompt_t + comp_t))
    return {
        "prompt_tokens": prompt_t,
        "completion_tokens": comp_t,
        "total_tokens": total_t,
    }


def _translate_gemini_json_to_openai(raw_bytes: bytes, model: str) -> bytes:
    data = json.loads(raw_bytes.decode("utf-8", errors="replace"))
    cand = (data.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or []

    text_chunks = []
    tool_calls = []
    ts_ms = int(time.time() * 1000)

    for idx, part in enumerate(parts):
        if part.get("thought") is True:
            continue
        if "text" in part and part["text"] is not None:
            text_chunks.append(str(part["text"]))
        elif "functionCall" in part:
            fc = part["functionCall"] or {}
            fn_name = fc.get("name", "unknown_tool")
            call_id = fc.get("id") or f"call_{fn_name}_{idx}_{ts_ms}"
            tc_entry: dict[str, Any] = {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": fn_name,
                    "arguments": json.dumps(fc.get("args") or {}),
                },
            }
            if part.get("thoughtSignature"):
                tc_entry["extra_content"] = {
                    "google": {"thought_signature": part["thoughtSignature"]}
                }
            tool_calls.append(tc_entry)

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_chunks),
    }
    if tool_calls:
        message["tool_calls"] = tool_calls

    resp_obj: dict[str, Any] = {
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": _map_finish_reason(cand.get("finishReason"), bool(tool_calls)),
        }],
    }
    usage = _map_usage(data.get("usageMetadata"))
    if usage:
        resp_obj["usage"] = usage
    return json.dumps(resp_obj).encode("utf-8")


class _SSEState:
    def __init__(self, model: str):
        self.model = model
        self.base_id = f"chatcmpl-{int(time.time())}"
        self.created = int(time.time())
        self.tool_idx = 0
        self.saw_tools = False
        self.last_finish: Optional[str] = None
        self.last_usage: Optional[dict] = None
        self.sent_role = False

    def process_line(self, line: str) -> bytes:
        line = line.strip()
        if not line or not line.startswith("data:"):
            return b""
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return b""

        try:
            data = json.loads(payload)
        except Exception:
            return b""

        if data.get("usageMetadata"):
            self.last_usage = _map_usage(data["usageMetadata"])

        cands = data.get("candidates") or []
        if not cands:
            return b""

        cand = cands[0]
        if cand.get("finishReason"):
            self.last_finish = cand["finishReason"]

        parts = (cand.get("content") or {}).get("parts") or []
        out_sse = []

        for part in parts:
            if part.get("thought") is True:
                continue
            if "text" in part and part["text"]:
                delta: dict[str, Any] = {"content": part["text"]}
                if not self.sent_role:
                    delta["role"] = "assistant"
                    self.sent_role = True
                chunk = {
                    "id": self.base_id,
                    "object": "chat.completion.chunk",
                    "created": self.created,
                    "model": self.model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                }
                out_sse.append(f"data: {json.dumps(chunk)}\n\n")
            elif "functionCall" in part:
                fc = part["functionCall"] or {}
                fn_name = fc.get("name", "unknown_tool")
                call_id = fc.get("id") or f"call_{fn_name}_{self.tool_idx}_{int(time.time() * 1000)}"
                tc_delta: dict[str, Any] = {
                    "index": self.tool_idx,
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": fn_name,
                        "arguments": json.dumps(fc.get("args") or {}),
                    },
                }
                if part.get("thoughtSignature"):
                    tc_delta["extra_content"] = {
                        "google": {"thought_signature": part["thoughtSignature"]}
                    }
                delta = {"tool_calls": [tc_delta]}
                if not self.sent_role:
                    delta["role"] = "assistant"
                    self.sent_role = True
                chunk = {
                    "id": self.base_id,
                    "object": "chat.completion.chunk",
                    "created": self.created,
                    "model": self.model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                }
                out_sse.append(f"data: {json.dumps(chunk)}\n\n")
                self.tool_idx += 1
                self.saw_tools = True

        return "".join(out_sse).encode("utf-8")

    def finalize(self) -> bytes:
        stop_chunk: dict[str, Any] = {
            "id": self.base_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": _map_finish_reason(self.last_finish, self.saw_tools),
            }],
        }
        if self.last_usage:
            stop_chunk["usage"] = self.last_usage
        return f"data: {json.dumps(stop_chunk)}\n\ndata: [DONE]\n\n".encode("utf-8")


class _GeminiSyncSSEStream(httpx.SyncByteStream):
    def __init__(self, client: httpx.Client, resp: httpx.Response, model: str):
        self._client = client
        self._resp = resp
        self._state = _SSEState(model)

    def __iter__(self) -> Iterator[bytes]:
        try:
            for line in self._resp.iter_lines():
                out = self._state.process_line(line)
                if out:
                    yield out
            yield self._state.finalize()
        finally:
            self.close()

    def close(self) -> None:
        try:
            self._resp.close()
        except Exception:
            pass
        try:
            self._client.close()
        except Exception:
            pass


class _GeminiAsyncSSEStream(httpx.AsyncByteStream):
    def __init__(self, client: httpx.AsyncClient, resp: httpx.Response, model: str):
        self._client = client
        self._resp = resp
        self._state = _SSEState(model)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for line in self._resp.aiter_lines():
                out = self._state.process_line(line)
                if out:
                    yield out
            yield self._state.finalize()
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        try:
            await self._resp.aclose()
        except Exception:
            pass
        try:
            await self._client.aclose()
        except Exception:
            pass


class GeminiAuthTransport(httpx.HTTPTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if _is_gemini_url(str(request.url)) and request.url.path.endswith("/chat/completions"):
            translated_req, is_stream, model = _translate_openai_to_gemini(request)
            client = httpx.Client(timeout=120.0, follow_redirects=True)
            if is_stream:
                resp = client.send(translated_req, stream=True)
                if resp.status_code >= 400:
                    err_bytes = resp.read()
                    resp.close()
                    client.close()
                    return httpx.Response(
                        status_code=resp.status_code,
                        headers={"content-type": "application/json"},
                        content=err_bytes,
                    )
                return httpx.Response(
                    status_code=200,
                    headers={"content-type": "text/event-stream"},
                    stream=_GeminiSyncSSEStream(client, resp, model),
                )
            try:
                resp = client.send(translated_req)
                if resp.status_code >= 400:
                    return httpx.Response(
                        status_code=resp.status_code,
                        headers={"content-type": "application/json"},
                        content=resp.content,
                    )
                return httpx.Response(
                    status_code=200,
                    headers={"content-type": "application/json"},
                    content=_translate_gemini_json_to_openai(resp.content, model),
                )
            finally:
                client.close()
        return super().handle_request(request)


class AsyncGeminiAuthTransport(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if _is_gemini_url(str(request.url)) and request.url.path.endswith("/chat/completions"):
            translated_req, is_stream, model = _translate_openai_to_gemini(request)
            client = httpx.AsyncClient(timeout=120.0, follow_redirects=True)
            if is_stream:
                resp = await client.send(translated_req, stream=True)
                if resp.status_code >= 400:
                    err_bytes = await resp.aread()
                    await resp.aclose()
                    await client.aclose()
                    return httpx.Response(
                        status_code=resp.status_code,
                        headers={"content-type": "application/json"},
                        content=err_bytes,
                    )
                return httpx.Response(
                    status_code=200,
                    headers={"content-type": "text/event-stream"},
                    stream=_GeminiAsyncSSEStream(client, resp, model),
                )
            try:
                resp = await client.send(translated_req)
                if resp.status_code >= 400:
                    return httpx.Response(
                        status_code=resp.status_code,
                        headers={"content-type": "application/json"},
                        content=resp.content,
                    )
                return httpx.Response(
                    status_code=200,
                    headers={"content-type": "application/json"},
                    content=_translate_gemini_json_to_openai(resp.content, model),
                )
            finally:
                await client.aclose()
        return await super().handle_async_request(request)


_prev_chat_openai_init = langchain_openai.ChatOpenAI.__init__


def _patched_gemini_chat_openai_init(self, *args, **kwargs):
    base_url = str(kwargs.get("base_url") or "")
    if _is_gemini_url(base_url):
        kwargs["base_url"] = _normalize_gemini_base(base_url)
        raw_timeout = kwargs.get("timeout")
        client_timeout = raw_timeout if isinstance(raw_timeout, (int, float, httpx.Timeout)) else 120.0
        if not kwargs.get("http_client"):
            kwargs["http_client"] = httpx.Client(transport=GeminiAuthTransport(), timeout=client_timeout)
        if not kwargs.get("async_client"):
            kwargs["async_client"] = httpx.AsyncClient(transport=AsyncGeminiAuthTransport(), timeout=client_timeout)
        kwargs.setdefault("use_responses_api", False)
    _prev_chat_openai_init(self, *args, **kwargs)


langchain_openai.ChatOpenAI.__init__ = _patched_gemini_chat_openai_init
