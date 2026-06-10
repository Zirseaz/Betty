"""Analyst Agent implementing a PolySwarm-style LLM ensemble for prediction markets."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select

from polyagent.agents.base import BaseAgent
from polyagent.config import Settings, LLMProvider
from polyagent.models import Market, Signal, SignalType, SignalStatus
from polyagent.utils.math import kl_divergence, implied_probability, calculate_ev

logger = logging.getLogger(__name__)

# ── LLM Prompts & Personas ─────────────────────────────────────────

PERSONAS = {
    "macro_economist": {
        "weight": 0.25,
        "instructions": (
            "You are a Macro Economist analyzing global markets, inflation, rates, and geopolitics. "
            "Evaluate this prediction market query from a structural, top-down perspective. "
            "Provide your estimated probability (0.0 to 1.0) of this event resolving YES, "
            "and a 2-sentence rationale."
        ),
    },
    "statistician": {
        "weight": 0.25,
        "instructions": (
            "You are a Mathematical Statistician and Quantitative Forecaster. "
            "Evaluate this prediction market query. Analyze base rates, historical frequencies, "
            "and statistical distributions. "
            "Provide your estimated probability (0.0 to 1.0) of this event resolving YES, "
            "and a 2-sentence rationale."
        ),
    },
    "contrarian": {
        "weight": 0.15,
        "instructions": (
            "You are a Contrarian Analyst and Crowd Sentiment expert. "
            "Evaluate this prediction market query. Identify typical cognitive biases, "
            "market overreactions, and consensus complacency. "
            "Provide your estimated probability (0.0 to 1.0) of this event resolving YES, "
            "and a 2-sentence rationale."
        ),
    },
    "news_analyst": {
        "weight": 0.20,
        "instructions": (
            "You are a Breaking News and Geopolitical Source Analyst. "
            "Evaluate this prediction market query. Consider recent headlines, source credibility, "
            "and immediate catalyst events. "
            "Provide your estimated probability (0.0 to 1.0) of this event resolving YES, "
            "and a 2-sentence rationale."
        ),
    },
    "bayesian_forecaster": {
        "weight": 0.15,
        "instructions": (
            "You are a Bayesian Forecaster. "
            "Establish a prior probability distribution based on general knowledge, "
            "then update it using the specific context of this market query. "
            "Provide your estimated probability (0.0 to 1.0) of this event resolving YES, "
            "and a 2-sentence rationale."
        ),
    },
}


class AnalystAgent(BaseAgent):
    """Ensemble analyst that queries LLM endpoints under different personas.
    
    Combines estimates, calculates KL divergence and EV, and generates signals.
    """

    name = "AnalystAgent"

    def __init__(self, settings: Settings) -> None:
        # Runs every 60 seconds by default
        super().__init__(settings, interval_seconds=60)
        self._analyzed_markets: dict[int, datetime] = {}

    async def setup(self) -> None:
        await super().setup()
        await self.log_action("started", {"provider": self.settings.llm_provider.value})

    async def run_cycle(self) -> None:
        if not self.settings.llm_api_key:
            logger.warning("[%s] LLM API key not set – skipping analysis cycle", self.name)
            return

        logger.info("[%s] Beginning analysis cycle...", self.name)
        
        # 1. Fetch active markets from database
        async with self._session_factory() as session:
            stmt = select(Market).where(Market.yes_price != None).order_by(Market.volume_24h.desc()).limit(10)
            res = await session.execute(stmt)
            markets = list(res.scalars().all())

        if not markets:
            logger.info("[%s] No active markets found in database to analyze", self.name)
            return

        # Filter out markets analyzed in the last 2 hours to avoid rate limit spam
        now = datetime.now(timezone.utc)
        candidate_markets = []
        for m in markets:
            last_analyzed = self._analyzed_markets.get(m.id)
            if last_analyzed and (now - last_analyzed).total_seconds() < 7200:
                continue
            candidate_markets.append(m)

        if not candidate_markets:
            logger.info("[%s] All active markets have been analyzed recently. Skipping.", self.name)
            return

        # Analyze the top market by volume first
        market_to_analyze = candidate_markets[0]
        logger.info("[%s] Analyzing market: %s", self.name, market_to_analyze.question)
        
        # 2. Run ensemble query
        analysis_results = await self._run_ensemble(market_to_analyze.question)
        self._analyzed_markets[market_to_analyze.id] = now
        
        if not analysis_results:
            logger.error("[%s] Ensemble analysis failed to return results", self.name)
            return

        # 3. Calculate weighted probability
        weighted_prob = 0.0
        details = {}
        for persona, result in analysis_results.items():
            prob = result.get("probability", 0.5)
            weight = PERSONAS[persona]["weight"]
            weighted_prob += prob * weight
            details[persona] = {
                "prob": prob,
                "rationale": result.get("rationale", ""),
            }

        # 4. Check edge & expected value
        market_prob = implied_probability(market_to_analyze.yes_price)
        edge = weighted_prob - market_prob
        abs_edge = abs(edge)

        # Log analysis outcome
        logger.info(
            "[%s] Market=%s | Price=%s | LLM Ensemble=%s | Raw Edge=%+s",
            self.name, market_to_analyze.id, market_to_analyze.yes_price,
            round(weighted_prob, 3), round(edge, 3)
        )
        
        await self.log_action("market_analyzed", {
            "market_id": market_to_analyze.id,
            "question": market_to_analyze.question,
            "market_price": market_to_analyze.yes_price,
            "ensemble_prob": weighted_prob,
            "edge": edge,
        })

        # Min edge configuration
        min_edge = float(self.settings.min_edge_pct) / 100.0

        if abs_edge >= min_edge:
            # We have a statistical edge! Determine side
            recommended_side = "buy" if edge > 0 else "sell"
            # In prediction markets, selling YES is equivalent to buying NO
            # We map this to buying the appropriate token
            token_to_buy = market_to_analyze.yes_token_id if edge > 0 else market_to_analyze.no_token_id
            target_price = market_to_analyze.yes_price if edge > 0 else market_to_analyze.no_price
            
            kl = kl_divergence(weighted_prob if edge > 0 else (1 - weighted_prob), target_price)
            ev = calculate_ev(weighted_prob if edge > 0 else (1 - weighted_prob), 1.0, target_price)
            
            # Confidence based on size of edge and KL divergence
            confidence = min(0.95, max(0.1, abs_edge * 2 + kl))

            async with self._session_factory() as session:
                signal = Signal(
                    market_id=market_to_analyze.id,
                    signal_type=SignalType.STATISTICAL,
                    edge_pct=abs_edge,
                    confidence=confidence,
                    data_json=json.dumps({
                        "estimated_prob": weighted_prob,
                        "market_prob": market_prob,
                        "side": recommended_side,
                        "kl_divergence": kl,
                        "expected_value": ev,
                        "target_token_id": token_to_buy,
                        "target_price": target_price,
                        "details": details,
                    }),
                    status=SignalStatus.PENDING,
                )
                session.add(signal)
                await session.commit()
                await session.refresh(signal)
                
                logger.info(
                    "[%s] Emitting STATISTICAL signal. Side=%s, Edge=%s, EV=%s",
                    self.name, recommended_side, round(abs_edge * 100, 2), round(ev, 3)
                )

                await self.emit_signal(
                    "signal_detected",
                    {
                        "signal_id": signal.id,
                        "market_id": market_to_analyze.id,
                        "question": market_to_analyze.question,
                        "type": SignalType.STATISTICAL.value,
                        "edge": abs_edge,
                        "confidence": confidence,
                        "target_token_id": token_to_buy,
                        "target_price": target_price,
                        "side": recommended_side,
                    }
                )

    async def _run_ensemble(self, question: str) -> dict[str, dict[str, Any]] | None:
        """Runs the LLM query in parallel across all personas."""
        tasks = []
        persona_names = list(PERSONAS.keys())
        for name in persona_names:
            tasks.append(self._query_llm(name, question))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        ensemble_results = {}
        for name, res in zip(persona_names, results):
            if isinstance(res, Exception):
                logger.error("[%s] Persona '%s' failed: %s", self.name, name, res)
                continue
            if res:
                ensemble_results[name] = res

        return ensemble_results if ensemble_results else None

    async def _query_llm(self, persona: str, question: str) -> dict[str, Any] | None:
        """Dispatches query to the configured LLM provider."""
        system_prompt = PERSONAS[persona]["instructions"]
        user_prompt = (
            f"Prediction Market Question: \"{question}\"\n\n"
            f"Analyze this market. You MUST return your response as a valid JSON object with the keys 'probability' and 'rationale'. "
            f"The 'probability' value must be a float between 0.0 and 1.0. The 'rationale' value must be a string containing a 2-sentence explanation."
        )

        provider = self.settings.llm_provider

        if provider == LLMProvider.OPENAI:
            return await self._call_openai(system_prompt, user_prompt)
        elif provider == LLMProvider.LOCAL:
            # Assume local OpenAI compatible API
            return await self._call_openai(system_prompt, user_prompt, base_url="http://localhost:11434/v1")
        elif provider == LLMProvider.DEEPSEEK:
            return await self._call_openai(system_prompt, user_prompt, base_url="https://api.deepseek.com", model="deepseek-chat")

        # Explicit Gemini support: only dispatch to Gemini if the Enum defines it
        # and the configured provider matches. Do NOT silently assume Gemini.
        gemini_member = getattr(LLMProvider, "GEMINI", None)
        if gemini_member is not None and provider == gemini_member:
            return await self._call_gemini(system_prompt, user_prompt)

        # Fallback: also accept providers whose value identifies as 'gemini'
        # (covers Enum definitions where the member name differs).
        provider_value = str(getattr(provider, "value", provider)).lower()
        if provider_value == "gemini":
            return await self._call_gemini(system_prompt, user_prompt)

        logger.error(
            "[%s] Unsupported LLM provider configured: %r. "
            "Supported providers: OPENAI, LOCAL, DEEPSEEK, GEMINI.",
            self.name, provider,
        )
        return None

    async def _call_openai(self, system_prompt: str, user_prompt: str, base_url: str | None = None, model: str = "gpt-4o-mini") -> dict[str, Any] | None:
        """Call OpenAI chat completion API."""
        try:
            # We import here to avoid dependency issues if openai package is not installed
            from openai import AsyncOpenAI
            
            client = AsyncOpenAI(api_key=self.settings.llm_api_key, base_url=base_url)
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
                max_tokens=150,
            )
            content = resp.choices[0].message.content
            if content:
                return json.loads(content)
        except Exception as e:
            logger.error("OpenAI call failed: %s", e)
        return None

    async def _call_gemini(self, system_prompt: str, user_prompt: str) -> dict[str, Any] | None:
        """Call Gemini API directly via HTTP."""
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"
        params = {"key": self.settings.llm_api_key}
        headers = {"Content-Type": "application/json"}
        
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": f"{system_prompt}\n\n{user_prompt}"}
                    ]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.2,
                "maxOutputTokens": 150,
            }
        }

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=payload, params=params, headers=headers, timeout=20.0)
                resp.raise_for_status()
                data = resp.json()
                
                # Extract text response
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                if text:
                    return json.loads(text)
        except Exception as e:
            logger.error("Gemini call failed: %s", e)
        return None
