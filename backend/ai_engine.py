"""
"""

from __future__ import annotations
import os
import json
import logging
import httpx
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Ensure environment variables are loaded from .env (with override=True)
load_dotenv(override=True)

logger = logging.getLogger("CallScreener.AIEngine")

class AIClassificationResult(BaseModel):
    action: str = "REJECT_AND_SMS" # REJECT_AND_SMS | VOICE_SCREEN | VIP_BYPASS | BLOCK_SPAM
    urgency_score: int = Field(default=3, ge=1, le=10)
    urgency_label: str = "LOW" # LOW | MEDIUM | HIGH | CRITICAL
    reasoning: str = ""
    reply_message: str = ""
    notify_user_immediately: bool = False
    tags: List[str] = Field(default_factory=list)

class AIEngine:
    """
    Modular AI decision engine supporting Ollama, Gemini API,
    and automatic deterministic fallback.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = config or {}
        llm_cfg = cfg.get("llm", {})
        self.provider = os.environ.get("LLM_PROVIDER") or llm_cfg.get("provider", "ollama")
        
        # Ollama config
        ollama_cfg = llm_cfg.get("ollama", {})
        self.ollama_base_url = os.environ.get("OLLAMA_BASE_URL") or ollama_cfg.get("base_url", "http://localhost:11434")
        self.ollama_model = os.environ.get("OLLAMA_MODEL") or ollama_cfg.get("model", "llama3.2")
        self.ollama_timeout = float(ollama_cfg.get("timeout", 15))

        # Gemini config
        gemini_cfg = llm_cfg.get("gemini", {})
        self.gemini_api_key = os.environ.get("GEMINI_API_KEY") or gemini_cfg.get("api_key", "")
        self.gemini_model = os.environ.get("GEMINI_MODEL") or gemini_cfg.get("model", "gemini-2.5-flash")

        # Sync both GEMINI_API_KEY and GOOGLE_API_KEY so official SDK picks the fresh key
        if self.gemini_api_key:
            os.environ["GEMINI_API_KEY"] = self.gemini_api_key
            os.environ["GOOGLE_API_KEY"] = self.gemini_api_key

        self.user_name = os.environ.get("USER_NAME") or cfg.get("app", {}).get("user_name", "Aditya")
        self.default_templates = cfg.get("default_templates", {})
        self.fallback_enabled = llm_cfg.get("fallback_to_template", True)

        logger.info(f"AI Engine initialized with primary provider: '{self.provider}' (Fallback enabled: {self.fallback_enabled})")

    # ------------------------------------------------------------
    # Intent & Context-Aware Auto-Reply Classifier
    # ------------------------------------------------------------
    async def classify_and_generate_reply(
        self,
        caller_number: str,
        caller_name: str,
        category: str,
        user_context: Dict[str, Any],
        screening_transcript: Optional[List[Dict[str, Any]]] = None
    ) -> AIClassificationResult:
        """
        Classifies call intent and urgency, and generates a context-aware personalized SMS reply.
        """
        status = user_context.get("status", "AVAILABLE")
        note = user_context.get("note", "")
        until = user_context.get("until", "")

        prompt = f"""
You are the personal AI Call Screener and Executive Secretary for {self.user_name}.
An incoming phone call has just arrived.

CALLER DETAILS:
- Name: {caller_name}
- Number: {caller_number}
- Contact Category: {category} (e.g. Family, Professor, Colleague, Delivery, Spam, Unknown)

{self.user_name.upper()}'S CURRENT LIVE CONTEXT:
- Status: {status}
- Activity/Reason: {note if note else 'Engaged'}
- Free After / Until: {until if until else 'Shortly'}

TRANSCRIPT (if screened):
{json.dumps(screening_transcript) if screening_transcript else 'No screening dialogue yet (direct call).'}

TASK:
1. Assess urgency (1 = lowest/spam, 10 = life-safety/critical emergency).
2. Assign urgency category: LOW (1-3), MEDIUM (4-6), HIGH (7-8), CRITICAL (9-10).
3. Recommend best action:
   - "VIP_BYPASS" (if immediate family or critical emergency that must ring now)
   - "REJECT_AND_SMS" (reject call and send tailored auto-reply)
   - "VOICE_SCREEN" (answer call via AI voice agent to screen)
   - "BLOCK_SPAM" (telemarketer/scam)
4. Draft a concise, professional, empathetic SMS message to send to the caller explaining why {self.user_name} is currently unavailable ({note or status}), when they might be available, and what they can do (e.g. text or call back later). Keep SMS under 160 characters if possible.

You MUST reply with ONLY valid JSON adhering strictly to this schema:
{{
  "action": "REJECT_AND_SMS",
  "urgency_score": 5,
  "urgency_label": "MEDIUM",
  "reasoning": "Brief 1-sentence reasoning",
  "reply_message": "Drafted SMS text here",
  "notify_user_immediately": false,
  "tags": ["work", "class"]
}}
"""

        # Try LLM providers
        raw_response = None
        if self.provider == "ollama":
            raw_response = await self._call_ollama(prompt)
            if not raw_response and self.gemini_api_key:
                logger.info("Ollama failed; falling back to Gemini API...")
                raw_response = await self._call_gemini(prompt)
        elif self.provider == "gemini":
            raw_response = await self._call_gemini(prompt)
            if not raw_response:
                logger.info("Gemini failed; falling back to Ollama...")
                raw_response = await self._call_ollama(prompt)

        # Parse LLM JSON
        if raw_response:
            parsed = self._extract_json(raw_response)
            if parsed:
                try:
                    return AIClassificationResult(**parsed)
                except Exception as e:
                    logger.warning(f"Error validating LLM output into schema: {e}")

        # Deterministic Template Fallback
        logger.info("Generating deterministic rule-based contextual reply.")
        return self._rule_based_fallback(caller_name, category, user_context)

    # ------------------------------------------------------------
    # Interactive Voice Screening Multi-Turn Agent
    # ------------------------------------------------------------
    async def generate_voice_screening_turn(
        self,
        caller_name: str,
        category: str,
        user_context: Dict[str, Any],
        conversation_history: List[Dict[str, str]],
        turn_number: int
    ) -> Dict[str, Any]:
        """
        Generates the next spoken response for the AI Voice Screener in Mode 2.
        Returns: {"speech_text": str, "should_end_call": bool, "action_note": str}
        """
        status = user_context.get("status", "AVAILABLE")
        note = user_context.get("note", "busy with commitments")

        if turn_number == 1 or not conversation_history:
            # Turn 1: AI Greeting & identity query
            greeting = f"Hello, I am {self.user_name}'s AI assistant. {self.user_name} is currently {note.lower() if note else 'busy'} and unable to take your call directly. May I know who is calling and how I may help you?"
            return {
                "speech_text": greeting,
                "should_end_call": False,
                "action_note": "Initial greeting delivered."
            }

        # Multi-turn conversation prompt
        prompt = f"""
You are {self.user_name}'s voice screening assistant answering a live phone call.
{self.user_name} is currently: {status} ({note}).

CALLER: {caller_name} ({category})
CONVERSATION SO FAR:
{json.dumps(conversation_history, indent=2)}

TASK:
Provide a natural, courteous, spoken response to the caller (maximum 2 short sentences).
Decide whether enough information has been collected to wrap up the call.
- If the caller stated their name and reason, acknowledge it politely and let them know {self.user_name} will receive the message.
- If they gave an urgent message, assure them {self.user_name} will be alerted right away.

Output strictly valid JSON:
{{
  "speech_text": "Spoken reply here",
  "should_end_call": true,
  "action_note": "Reason for ending or continuing"
}}
"""
        raw = None
        if self.provider == "ollama":
            raw = await self._call_ollama(prompt)
        elif self.provider == "gemini":
            raw = await self._call_gemini(prompt)

        if raw:
            data = self._extract_json(raw)
            if data and "speech_text" in data:
                return data

        # Deterministic conversational fallback
        last_caller_msg = conversation_history[-1].get("message", "") if conversation_history else ""
        if "urgent" in last_caller_msg.lower() or "emergency" in last_caller_msg.lower():
            return {
                "speech_text": f"I understand this is urgent. I have flagged your message as high priority and notified {self.user_name} immediately. Thank you for calling.",
                "should_end_call": True,
                "action_note": "Urgent flag acknowledged"
            }
        else:
            return {
                "speech_text": f"Thank you for the message. I have recorded your note and {self.user_name} will get back to you as soon as they are free. Have a great day!",
                "should_end_call": True,
                "action_note": "Call completed"
            }

    # ------------------------------------------------------------
    # Call Summary & Action Item Extractor
    # ------------------------------------------------------------
    async def summarize_call(
        self,
        caller_name: str,
        caller_number: str,
        transcript: List[Dict[str, str]]
    ) -> str:
        """Generates a concise executive bulleted summary of the call transcript."""
        if not transcript:
            return "Call ended without spoken conversation."

        text_transcript = "\n".join([f"{t.get('role', 'speaker')}: {t.get('message', '')}" for t in transcript])
        prompt = f"""
Summarize this call screened by {self.user_name}'s AI screener.
Caller: {caller_name} ({caller_number})

TRANSCRIPT:
{text_transcript}

Provide a concise 1-2 sentence executive summary with caller intent and any required action items or deadlines.
"""
        summary = None
        if self.provider == "ollama":
            summary = await self._call_ollama(prompt)
        elif self.provider == "gemini":
            summary = await self._call_gemini(prompt)

        if summary:
            return summary.strip()

        # Fallback summary
        return f"Call with {caller_name}. Screener logged transcript with {len(transcript)} dialogue turns."

    # ------------------------------------------------------------
    # Provider Implementations
    # ------------------------------------------------------------
    async def _call_ollama(self, prompt: str) -> Optional[str]:
        """Queries local Ollama instance via HTTP API."""
        url = f"{self.ollama_base_url.rstrip('/')}/api/generate"
        payload = {
            "model": self.ollama_model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.2
            }
        }
        try:
            async with httpx.AsyncClient(timeout=self.ollama_timeout) as client:
                res = await client.post(url, json=payload)
                if res.status_code == 200:
                    data = res.json()
                    return data.get("response", "")
                else:
                    logger.warning(f"Ollama returned HTTP {res.status_code}: {res.text}")
        except Exception as e:
            logger.debug(f"Ollama connection error: {e}")
        return None

    async def _call_gemini(self, prompt: str) -> Optional[str]:
        """Queries Google Gemini API via google-genai SDK or REST API."""
        if not self.gemini_api_key:
            return None

        # Strategy 1: Official google-genai SDK (Async/Sync execution)
        try:
            from google import genai
            client = genai.Client(api_key=self.gemini_api_key)
            # Run in thread pool to avoid blocking asyncio loop
            # pyrefly: ignore[unknown-name]
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                lambda: client.models.generate_content(
                    model=self.gemini_model,
                    contents=prompt
                )
            )
            if response and response.text:
                return response.text
        except Exception as sdk_err:
            logger.debug(f"google-genai SDK call notice: {sdk_err}, trying REST endpoint...")

        # Strategy 2: Direct REST HTTP fallback
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.gemini_model}:generateContent?key={self.gemini_api_key}"
        payload = {
            "contents": [{
                "parts": [{"text": prompt}]
            }],
            "generationConfig": {
                "temperature": 0.2,
                "responseMimeType": "application/json"
            }
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.post(url, json=payload)
                if res.status_code == 200:
                    data = res.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if parts:
                            return parts[0].get("text", "")
                else:
                    logger.warning(f"Gemini API returned HTTP {res.status_code}: {res.text}")
        except Exception as e:
            logger.warning(f"Gemini API connection error: {e}")
        return None

    # ------------------------------------------------------------
    # Deterministic Rule-Based Fallback Engine
    # ------------------------------------------------------------
    def _rule_based_fallback(self, caller_name: str, category: str, user_context: Dict[str, Any]) -> AIClassificationResult:
        """High-grade template generator when LLMs are offline."""
        status = user_context.get("status", "AVAILABLE")
        note = user_context.get("note", "")
        until = user_context.get("until", "")
        clean_name = caller_name if caller_name and caller_name != "Unknown" else "there"

        # Check Category First
        if category == "Spam":
            return AIClassificationResult(
                action="BLOCK_SPAM",
                urgency_score=1,
                urgency_label="LOW",
                reasoning="Identified telemarketing/spam number.",
                reply_message="Notice: Your call has been screened and rejected.",
                notify_user_immediately=False,
                tags=["spam"]
            )

        if category == "Emergency":
            return AIClassificationResult(
                action="VIP_BYPASS",
                urgency_score=10,
                urgency_label="CRITICAL",
                reasoning="Emergency contact - allowing immediate bypass.",
                reply_message=f"Hi {clean_name}, {self.user_name} has been notified of your emergency call.",
                notify_user_immediately=True,
                tags=["emergency", "critical"]
            )

        # Context-based custom message
        if status == "IN_CLASS":
            time_part = f" until {until}" if until else ""
            msg = f"Hi {clean_name}, {self.user_name} is in class/lecture{time_part} ({note or 'Academic Session'}). Please text if urgent."
            score = 6 if category in ["Professor", "Family"] else 3
        elif status == "CODING_FOCUS":
            msg = f"Hi {clean_name}, {self.user_name} is in a deep coding block ({note or 'Sprint'}). Your call is noted; he will check messages shortly."
            score = 5 if category in ["Colleague", "Work"] else 3
        elif status == "SLEEPING":
            msg = f"Hi {clean_name}, {self.user_name} is resting with DND enabled. If this is an emergency, reply 'EMERGENCY'."
            score = 7 if category == "Family" else 2
        elif status == "DND":
            msg = f"Hi {clean_name}, {self.user_name} currently has Do Not Disturb active. Your call has been logged."
            score = 3
        else: # BUSY / AVAILABLE
            msg = f"Hi {clean_name}, {self.user_name} is currently busy ({note or 'in a meeting'}). He will call you back as soon as possible."
            score = 4

        # Custom adjustment for Delivery
        if category == "Delivery":
            msg = f"Hello! {self.user_name} is currently occupied. Please leave the parcel at the door / with security. Thank you!"
            score = 5

        label = "CRITICAL" if score >= 9 else "HIGH" if score >= 7 else "MEDIUM" if score >= 4 else "LOW"

        return AIClassificationResult(
            action="REJECT_AND_SMS",
            urgency_score=score,
            urgency_label=label,
            reasoning=f"Automatic contextual rule applied for {status}.",
            reply_message=msg,
            notify_user_immediately=(score >= 7),
            tags=[status.lower(), category.lower()]
        )

    def _extract_json(self, text: str) -> Optional[Dict[str, Any]]:
        """Cleans and extracts JSON object from raw LLM output text."""
        if not text:
            return None
        text = text.strip()
        # Remove markdown codeblocks ```json ... ```
        if "```" in text:
            parts = text.split("```")
            for part in parts:
                clean_part = part.strip()
                if clean_part.startswith("json"):
                    clean_part = clean_part[4:].strip()
                if clean_part.startswith("{") and clean_part.endswith("}"):
                    try:
                        return json.loads(clean_part)
                    except Exception:
                        pass

        # Try direct parse
        try:
            return json.loads(text)
        except Exception:
            pass

        # Extract between first { and last }
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end+1])
            except Exception:
                pass
        return None
