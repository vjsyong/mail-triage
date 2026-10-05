"""MiniCPM5 native template reference renderer (acceptance AC5).

This mirrors the released ``chat_template.jinja`` of ``openbmb/MiniCPM5-2B`` so
the slice can be tested offline without a tokenizer, and so committed examples
are inspectable.  Training itself uses the tokenizer's **own**
``apply_chat_template`` (the authority); this module is the faithful reference.

Preserved: the ``tool`` role (a ``<tool_response>`` block), ``<function ...>``
XML tool calls with CDATA for multi-line values, and ``<think>`` reasoning
markers.  It is *not* the upstream chat-only SFT patch that drops tool roles.
"""
from __future__ import annotations

import json
import re

BOS = "<s>"
EOS = "</s>"
IM_START = "<|im_start|>"
IM_END = "<|im_end|>"

_TOOL_GUIDELINES = (
    "\n</tools>\n\nTool usage guidelines:\n"
    "- You may call zero or more functions. If no function calls are needed, "
    "just answer normally and do not include any <function ... </function>.\n"
    "- When calling a function, return an XML object within "
    "<function ... </function> using:\n"
    '<function name="function-name"><param name="param-name">param-value'
    "</param></function>\n"
    "- param-value may be multi-line. If it contains <, & or newline characters, "
    'wrap it in a CDATA block: <param name="param-name"><![CDATA[...'
    "multi-line value...]]></param>"
)

_FUNCTION_RE = re.compile(r"<function name=\"([^\"]+)\">(.*?)</function>", re.S)
_PARAM_RE = re.compile(r"<param name=\"([^\"]+)\">(.*?)</param>", re.S)
_CDATA_RE = re.compile(r"^<!\[CDATA\[(.*)\]\]>$", re.S)


def encode_argument_value(value):
    """Encode one tool-call argument value for the XML template.

    Strings pass through (the template adds CDATA when needed); every other JSON
    type becomes a compact JSON string (``true``/``false``, numbers, arrays and
    nested objects) so the round-trip is lossless.
    """
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def render_tool_call(name, arguments):
    """Render one assistant tool call as MiniCPM ``<function>`` XML."""
    parts = ['<function name="%s">' % name]
    for param_name, param_value in (arguments or {}).items():
        text = encode_argument_value(param_value)
        parts.append('<param name="%s">' % param_name)
        if any(ch in text for ch in ("<", "&", "\n")):
            parts.append("<![CDATA[%s]]>" % text)
        else:
            parts.append(text)
        parts.append("</param>")
    parts.append("</function>")
    return "".join(parts)


def _tool_definitions(tools):
    body = ("# Tools\n\nYou are provided with function signatures within "
            "<tools></tools> XML tags:\n<tools>")
    for tool in tools:
        # sorted keys make the reference render stable across JSON round-trips so
        # render(example.messages) == the committed native_render (S3).
        body += "\n" + json.dumps(tool, ensure_ascii=False, sort_keys=True)
    return body + _TOOL_GUIDELINES


def _render_assistant(message):
    content = message.get("content") or ""
    reasoning = message.get("reasoning_content")
    if reasoning is None and "</think>" in content:
        reasoning = content.split("</think>")[0].rstrip("\n").split("<think>")[-1].lstrip("\n")
        content = content.split("</think>")[-1].lstrip("\n")
    out = [IM_START + "assistant\n"]
    if reasoning:
        out.append("<think>\n%s\n</think>\n\n%s" % (reasoning.strip("\n"),
                                                    content.lstrip("\n")))
    elif "<think>" not in content and "</think>" not in content:
        out.append("<think>\n\n</think>\n\n" + content.lstrip("\n"))
    else:
        out.append(content)
    calls = message.get("tool_calls") or []
    for i, tc in enumerate(calls):
        fn = tc.get("function") if isinstance(tc, dict) and "function" in tc else tc
        name = fn.get("name")
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        first_content = (i == 0 and bool(content and content.strip()))
        out.append("\n" if (first_content or i > 0) else "")
        out.append(render_tool_call(name, args))
    out.append(IM_END + "\n")
    return "".join(out)


def render_messages(messages, tools=None, add_generation_prompt=False,
                    enable_thinking=False):
    """Render a canonical/native message list with the MiniCPM5 template."""
    messages = list(messages or [])
    out = [BOS]
    if tools:
        tool_defs = _tool_definitions(tools)
        out.append(IM_START + "system\n")
        if messages and messages[0].get("role") == "system":
            content = messages[0].get("content") or ""
            if "<tool_def_sep>" in content:
                out.append(content.replace("<tool_def_sep>", tool_defs))
            else:
                out.append(content + "\n\n" + tool_defs)
        else:
            out.append(tool_defs.lstrip())
        out.append(IM_END + "\n")
    elif messages and messages[0].get("role") == "system":
        out.append(IM_START + "system\n" + (messages[0].get("content") or "")
                   + IM_END + "\n")

    for i, message in enumerate(messages):
        role = message.get("role")
        content = message.get("content") if isinstance(message.get("content"), str) else ""
        if role == "user" or (role == "system" and i != 0):
            out.append(IM_START + role + "\n" + content + IM_END + "\n")
        elif role == "assistant":
            out.append(_render_assistant(message))
        elif role == "tool":
            prev_tool = i > 0 and messages[i - 1].get("role") == "tool"
            next_tool = i + 1 < len(messages) and messages[i + 1].get("role") == "tool"
            if not prev_tool:
                out.append(IM_START + "user")
            out.append("\n<tool_response>\n")
            out.append(content if content else json.dumps(message.get("content"),
                                                          ensure_ascii=False))
            out.append("\n</tool_response>")
            if not next_tool:
                out.append(IM_END + "\n")

    if add_generation_prompt:
        out.append(IM_START + "assistant\n")
        if not enable_thinking:
            out.append("<think>\n\n</think>\n\n")
        else:
            out.append("<think>\n")
    return "".join(out)


def _decode_param_value(text):
    """Decode one ``<param>`` value back to a JSON-ish Python value."""
    m = _CDATA_RE.match(text)
    if m:
        text = m.group(1)
    try:
        val = json.loads(text)
    except (TypeError, ValueError):
        return text
    return val if not isinstance(val, str) else text


def parse_tool_calls(text):
    """Parse MiniCPM ``<function>`` XML back into tool calls.

    Nested rule arguments, booleans, arrays and multi-line strings round-trip.
    Each call keeps both the parsed ``arguments`` dict and the ``raw_arguments``
    string so a trace records exactly what the model emitted.
    """
    calls = []
    for name, body in _FUNCTION_RE.findall(text or ""):
        args = {}
        for pname, pvalue in _PARAM_RE.findall(body):
            args[pname] = _decode_param_value(pvalue)
        calls.append({
            "id": "call_%s_%d" % (name, len(calls)),
            "name": name,
            "arguments": args,
            "raw_arguments": body,
        })
    return calls
