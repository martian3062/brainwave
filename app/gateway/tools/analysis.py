"""Two tools that exist as a matched pair, to show why `upto` has to exist.

`summarize_bug_report` is deterministic. It parses a report and extracts what is
in it. The work is bounded by the input, the cost is known before it runs, and
so it is priced `exact` at $0.001 flat and captures exactly that.

`analyze_contract` calls a model. Nobody -- not the caller, not this server, not
the model -- knows what it will cost until it has finished, because the answer's
length is part of the question. Pricing that `exact` means either overcharging
every short answer or losing money on every long one. So it is priced `upto`: the
agent authorizes a $0.05 ceiling, the tool reports its real token consumption
through `app.gateway.meter`, and only that is captured. The receipt shows the
ceiling and the charge side by side.

That contrast is the whole reason metering is in this project. One tool would
not have made the point.

--------------------------------------------------------------------------
CLAUDE API NOTES (verified against the current API, not recalled)
--------------------------------------------------------------------------
* Model is `claude-opus-5`. Thinking is ON BY DEFAULT on this model -- omitting
  the `thinking` parameter runs adaptive, unlike Opus 4.8/4.7 where omitting it
  meant no thinking. `max_tokens` caps thinking AND response text together, so
  the budget below is sized for both.
* `temperature`, `top_p`, `top_k` are REMOVED on this model and return 400.
  There are none in this file; steering is done with the prompt.
* Safety classifiers can decline a request: HTTP 200 with
  `stop_reason == "refusal"`. `response.content` may be empty, so `stop_reason`
  is checked BEFORE the content is read. A refusal is billed at zero when it
  happens before any output -- and it captures nothing here either.
* Server-side fallbacks (`fallbacks="default"`) re-serve a declined request on
  another model inside the same call. It is a beta and the SDK's typing lags it,
  so it is sent and, on a 400 that names it, dropped and retried once. Degrading
  is better than failing a paid call over an optional feature.
* Token counts come from `response.usage`, which is what actually happened. No
  client-side estimator is used -- `tiktoken` is OpenAI's tokenizer and would
  undercount Claude by 15-20%, which on a metered tool is not an estimate error
  but a billing error.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from mcp.server.fastmcp import FastMCP

from app.config import settings
from app.gateway import meter
from app.gateway.config import gateway_settings as gw
from app.gateway.ledger import ToolSpec
from app.gateway.paid import paid
from app.models import Scheme
from app.money import format_atomic

log = logging.getLogger(__name__)

__all__ = ["register"]

#: Atomic units per token, floored from the configured per-1k price. Floored
#: rather than rounded so the effective price can only ever be at or below what
#: was configured -- a rounding error must not become an overcharge.
_PER_TOKEN_ATOMIC = max(gw.analyze_per_ktok_atomic // 1_000, 0)
_EFFECTIVE_PER_KTOK = _PER_TOKEN_ATOMIC * 1_000
_ANALYZE_CEILING = format_atomic(
    gw.analyze_max_atomic,
    settings.x402_asset_decimals,
    symbol=settings.x402_asset_symbol,
)


ANALYZE = ToolSpec(
    name="analyze_contract",
    description=(
        "LLM security review of a smart contract or agent-tool source. Returns findings "
        "with severity and reasoning. Priced `upto`: you authorize a ceiling of "
        f"{_ANALYZE_CEILING}"
        " and only measured token consumption is captured -- the receipt reports both "
        "the ceiling and the charge. Requires ANTHROPIC_API_KEY on the server; without "
        "it the tool reports itself unavailable and captures nothing."
    ),
    price_atomic=gw.analyze_base_atomic,
    max_price_atomic=gw.analyze_max_atomic,
    scheme=Scheme.UPTO,
    meter="tokens",
    price_per_unit_atomic=_PER_TOKEN_ATOMIC,
    tags=("llm", "security", "audit", "metered"),
    max_timeout_seconds=300,
    rationale=(
        f"Base {format_atomic(gw.analyze_base_atomic, settings.x402_asset_decimals)} plus "
        f"{format_atomic(_EFFECTIVE_PER_KTOK, settings.x402_asset_decimals)} per 1k tokens "
        "(input + output), clamped at the ceiling."
    ),
)

SUMMARIZE = ToolSpec(
    name="summarize_bug_report",
    description=(
        "Structure a free-text bug report: extract the error type and message, stack "
        "frames, reproduction steps, environment lines and affected files, and score "
        "severity. Deterministic and offline -- no model call, which is why it can be "
        "priced `exact` and always costs the same."
    ),
    price_atomic=gw.summarize_atomic,
    scheme=Scheme.EXACT,
    tags=("triage", "parsing", "deterministic"),
    rationale="Bounded parsing over the input; cost is known before it runs.",
)


# --------------------------------------------------------------------------
# Deterministic report structuring
# --------------------------------------------------------------------------

_EXCEPTION_RE = re.compile(
    r"^(?P<type>[A-Z][A-Za-z0-9_.]*(?:Error|Exception|Warning|Fault))\s*:\s*(?P<msg>.+)$",
    re.MULTILINE,
)
_FRAME_RE = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>\S+))?')
_JS_FRAME_RE = re.compile(r"at\s+(?P<func>[\w.<>$]+)\s+\((?P<file>[^):]+):(?P<line>\d+)")
_STEP_RE = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+(?P<step>.+\S)\s*$", re.MULTILINE)
_ENV_RE = re.compile(
    r"^\s*(python|node|os|platform|version|browser|runtime|arch)\b[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)

_SEVERITY_SIGNALS = {
    "critical": ("data loss", "corruption", "security", "rce", "exploit", "credential", "leak"),
    "high": ("crash", "panic", "segfault", "deadlock", "outage", "500", "unhandled"),
    "medium": ("regression", "incorrect", "wrong", "fails", "timeout", "flaky"),
}


def _structure_report(text: str) -> dict[str, Any]:
    lowered = text.lower()

    exception = None
    match = _EXCEPTION_RE.search(text)
    if match:
        exception = {"type": match.group("type"), "message": match.group("msg").strip()[:400]}

    frames = [
        {"file": m.group("file"), "line": int(m.group("line")), "function": m.group("func")}
        for m in _FRAME_RE.finditer(text)
    ] or [
        {"file": m.group("file"), "line": int(m.group("line")), "function": m.group("func")}
        for m in _JS_FRAME_RE.finditer(text)
    ]

    steps = [m.group("step")[:200] for m in _STEP_RE.finditer(text)][:20]
    environment = [line.strip()[:160] for line in _ENV_RE.findall(text)][:10]

    severity, signal = "low", None
    for level, needles in _SEVERITY_SIGNALS.items():
        hit = next((n for n in needles if n in lowered), None)
        if hit:
            severity, signal = level, hit
            break

    files = sorted({f["file"] for f in frames})
    return {
        "ok": True,
        "engine": "deterministic",
        "severity": severity,
        "severitySignal": signal,
        "exception": exception,
        "stackFrames": frames[:25],
        "affectedFiles": files[:25],
        "reproductionSteps": steps,
        "environment": environment,
        "lineCount": text.count("\n") + 1,
        "charCount": len(text),
        "note": (
            "Structural extraction only -- no model was called, so this is a parse of "
            "what the report says, not a judgement about the bug."
        ),
    }


# --------------------------------------------------------------------------
# LLM review
# --------------------------------------------------------------------------

_SYSTEM = (
    "You are a security reviewer for smart contracts and agent tool code. "
    "Report concrete findings only. For each finding give: severity "
    "(critical|high|medium|low), a one-line title, the affected symbol or line, "
    "why it is exploitable, and the fix. If the code is sound, say so plainly and "
    "do not invent findings. Do not restate the code back. Be concise."
)


def _unavailable(reason: str, hint: str) -> dict[str, Any]:
    """A structured non-answer that captures nothing.

    `meter.record` is deliberately NOT called, so metered capture stays at the
    base price -- and the base price is what the tool costs to have been
    offered, not what an answer costs. Charging the ceiling for an unavailable
    dependency would be the exact behaviour this whole project argues against.
    """
    return {
        "ok": False,
        "engine": "unavailable",
        "error": reason,
        "hint": hint,
        "charged": "base price only; no tokens were consumed and none were metered",
    }


async def _analyze(source: str, focus: str) -> dict[str, Any]:
    if not gw.llm_configured:
        return _unavailable(
            "no LLM credentials configured on this gateway",
            "set ANTHROPIC_API_KEY in the server environment",
        )
    try:
        import anthropic
    except ImportError:
        return _unavailable(
            "the anthropic SDK is not installed in this deployment",
            "pip install anthropic (it is pinned in requirements.txt)",
        )

    client = anthropic.AsyncAnthropic(
        api_key=gw.anthropic_api_key, timeout=gw.analysis_timeout, max_retries=1
    )
    prompt = (
        f"Focus: {focus}\n\nReview the following source for security issues.\n\n```\n{source}\n```"
    )
    request: dict[str, Any] = {
        "model": gw.analysis_model,
        # Caps thinking + response together on this model. Adaptive thinking is
        # on by default here, so this has to leave room for both.
        "max_tokens": gw.analysis_max_tokens,
        "system": _SYSTEM,
        "output_config": {"effort": gw.analysis_effort},
        "messages": [{"role": "user", "content": prompt}],
    }

    try:
        response = await _create_with_fallbacks(client, anthropic, request)
    except Exception as exc:
        log.warning("analyze_contract model call failed: %s", exc)
        return _unavailable(
            f"model call failed: {str(exc)[:300]}", "retry, or lower the input size"
        )
    finally:
        await client.close()

    # Check stop_reason BEFORE reading content: on a refusal the content list
    # can be empty and `content[0]` would raise.
    if getattr(response, "stop_reason", None) == "refusal":
        details = getattr(response, "stop_details", None)
        return _unavailable(
            "the model declined this request",
            f"refusal category: {getattr(details, 'category', None)}",
        )

    text = "".join(
        block.text for block in (response.content or []) if getattr(block, "type", "") == "text"
    ).strip()

    usage = getattr(response, "usage", None)
    billed = _billable_tokens(usage)
    # THE METERING CALL. Everything about `upto` in this project reduces to this
    # one line: the tool reports what it consumed, and the payment layer reads
    # it at settle time.
    meter.record(
        billed,
        model=getattr(response, "model", gw.analysis_model),
        inputTokens=getattr(usage, "input_tokens", None),
        outputTokens=getattr(usage, "output_tokens", None),
        cacheReadTokens=getattr(usage, "cache_read_input_tokens", None),
        stopReason=getattr(response, "stop_reason", None),
    )

    return {
        "ok": True,
        "engine": "claude",
        "model": getattr(response, "model", gw.analysis_model),
        "focus": focus,
        "findings": text or "(the model returned no text)",
        "usage": {
            "inputTokens": getattr(usage, "input_tokens", None),
            "outputTokens": getattr(usage, "output_tokens", None),
            "cacheReadInputTokens": getattr(usage, "cache_read_input_tokens", None),
            "billedTokens": billed,
        },
        "metering": {
            "unit": "tokens",
            "pricePerKTokenAtomic": str(_EFFECTIVE_PER_KTOK),
            "note": (
                "Capture = base + billed tokens x per-token price, clamped at the "
                "authorized ceiling. See the receipt for the amount actually taken."
            ),
        },
    }


def _billable_tokens(usage: Any) -> int:
    """What we charge for: real input + real output, from the response.

    Cache reads are counted at full weight rather than discounted. That is
    deliberately simple and deliberately in the payer's favour to state plainly
    rather than model badly -- and cache reads only occur on repeated prefixes,
    which this tool does not currently build.
    """
    if usage is None:
        return 0
    total = int(getattr(usage, "input_tokens", 0) or 0) + int(
        getattr(usage, "output_tokens", 0) or 0
    )
    return max(total, 0)


async def _create_with_fallbacks(client: Any, anthropic_mod: Any, request: dict[str, Any]) -> Any:
    """Send with server-side refusal fallbacks; drop them if they are rejected.

    `fallbacks="default"` routes a policy decline to Anthropic's recommended
    substitute model inside the same call, which matters here because a security
    review is exactly the kind of prompt a cyber classifier may decline. It is
    beta, so a deployment whose SDK or account does not have it gets a 400 --
    and a paid tool must not fail over an optional feature.
    """
    try:
        return await client.beta.messages.create(
            **request,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except Exception as exc:
        if not isinstance(exc, getattr(anthropic_mod, "BadRequestError", ())):
            raise
        log.info("server-side fallbacks unavailable (%s); retrying without", exc)
        return await client.messages.create(**request)


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


def register(mcp: FastMCP) -> None:
    @mcp.tool(name=ANALYZE.name, description=ANALYZE.description)
    @paid(
        ANALYZE,
        input_schema={
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Source code to review."},
                "focus": {
                    "type": "string",
                    "description": "What to weight, e.g. 'reentrancy' or 'access control'.",
                },
            },
            "required": ["source"],
        },
        example={"source": "contract X { ... }", "focus": "reentrancy"},
    )
    async def analyze_contract(source: str, focus: str = "general security review") -> dict:
        source = source or ""
        if not source.strip():
            return _unavailable("source is empty", "pass the code to review as `source`")
        if len(source) > gw.analysis_max_input_chars:
            # Refused rather than truncated. Silently reviewing two thirds of a
            # contract and charging the ceiling for it is the worst outcome
            # available; a caller told the limit can split the input.
            return _unavailable(
                f"source is {len(source)} chars, over the {gw.analysis_max_input_chars} limit",
                "split the input and review it in parts",
            )
        return await _analyze(source, (focus or "general security review")[:200])

    @mcp.tool(name=SUMMARIZE.name, description=SUMMARIZE.description)
    @paid(
        SUMMARIZE,
        input_schema={
            "type": "object",
            "properties": {"report": {"type": "string", "description": "The raw bug report text."}},
            "required": ["report"],
        },
        example={"report": "TypeError: x is not a function\n  at foo (app.js:12)"},
    )
    async def summarize_bug_report(report: str) -> dict:
        report = (report or "")[:200_000]
        if not report.strip():
            return {"ok": False, "error": "report is empty"}
        return _structure_report(report)
