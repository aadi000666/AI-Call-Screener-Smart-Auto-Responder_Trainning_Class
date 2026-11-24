"""
AI Call Screener & Smart Auto-Responder - Voice Agent Pipeline
Integrates natural edge-tts synthesis, SpeechRecognition STT,
and real-time interactive screening sessions.
"""

from __future__ import annotations
import os
import uuid
import asyncio
import logging
from typing import Dict, Any, List, Optional, Callable, Awaitable
from datetime import datetime, timezone

logger = logging.getLogger("CallScreener.Voice")

class VoicePipeline:
    """
    Handles speech synthesis (TTS via edge-tts with gTTS fallback)
    and transcription (STT via SpeechRecognition).
    """

    def __init__(
        self,
        voice: str = "en-IN-NeerjaNeural",
        rate: str = "+0%",
        pitch: str = "+0Hz",
        output_dir: str = "data/audio_logs"
    ):
        self.voice = voice
        self.rate = rate
        self.pitch = pitch
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)

    async def synthesize(self, text: str, filename_prefix: str = "tts") -> str:
        """
        Synthesizes text into an MP3 audio file using edge-tts.
        Falls back to gTTS if edge-tts fails (e.g. offline/network blocked).
        Returns relative file path of the generated audio file.
        """
        clean_text = text.strip()
        filename = f"{filename_prefix}_{uuid.uuid4().hex[:8]}.mp3"
        filepath = os.path.join(self.output_dir, filename)

        # 1. Try edge-tts
        try:
            import edge_tts
            communicate = edge_tts.Communicate(clean_text, self.voice, rate=self.rate, pitch=self.pitch)
            await communicate.save(filepath)
            if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                logger.info(f"Synthesized speech via edge-tts ({self.voice}) -> {filepath}")
                return filepath
        except Exception as e:
            logger.warning(f"edge-tts synthesis error ({e}), attempting fallback...")

        # 2. Try gTTS fallback
        try:
            from gtts import gTTS
            lang = "hi" if "hi-" in self.voice.lower() else "en"
            tts = gTTS(text=clean_text, lang=lang, slow=False)
            tts.save(filepath)
            if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                logger.info(f"Synthesized speech via gTTS fallback -> {filepath}")
                return filepath
        except Exception as e:
            logger.warning(f"gTTS fallback error ({e}).")

        # 3. Dummy audio placeholder if offline/blocked
        # Writes an empty or tiny audio file so the pipeline won't crash
        try:
            with open(filepath, "wb") as f:
                f.write(b"") # Placeholder
            return filepath
        except Exception:
            return ""

    def transcribe_file(self, audio_path: str) -> str:
        """
        Transcribes a WAV/audio file to text using SpeechRecognition.
        """
        if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
            return ""

        try:
            import speech_recognition as sr
            r = sr.Recognizer()
            with sr.AudioFile(audio_path) as source:
                audio_data = r.record(source)
                text = r.recognize_google(audio_data)
                return text
        except Exception as e:
            logger.warning(f"Transcription failed: {e}")
            return ""


class VoiceScreeningSession:
    """
    Manages an active interactive screening call session (Mode 2).
    Coordinates conversation turns between AI voice agent and caller.
    """

    def __init__(
        self,
        call_id: str,
        caller_number: str,
        caller_name: str,
        category: str,
        user_context: Dict[str, Any],
        ai_engine: Any,
        voice_pipeline: VoicePipeline,
        on_event: Optional[Callable[[str, Dict[str, Any]], Awaitable[None]]] = None,
        on_audio_ready: Optional[Callable[[str], None]] = None
    ):
        self.call_id = call_id
        self.caller_number = caller_number
        self.caller_name = caller_name
        self.category = category
        self.user_context = user_context
        self.ai_engine = ai_engine
        self.voice_pipeline = voice_pipeline
        self.on_event = on_event
        self.on_audio_ready = on_audio_ready

        self.transcript: List[Dict[str, Any]] = []
        self.turn_count = 0
        self.max_turns = 3
        self.is_active = True
        self.audio_files: List[str] = []
        self.start_time = datetime.now(timezone.utc)

    async def _emit(self, event_type: str, data: Dict[str, Any]):
        """Dispatches real-time WebSocket events for the live UI."""
        if self.on_event:
            try:
                await self.on_event(event_type, {
                    "call_id": self.call_id,
                    "caller_number": self.caller_number,
                    "caller_name": self.caller_name,
                    **data
                })
            except Exception as e:
                logger.error(f"Error in session event emitter: {e}")

    async def start_session(self) -> str:
        """
        Starts the session by answering the call, greeting the caller,
        and prompting for their purpose.
        Returns the initial greeting audio path.
        """
        self.turn_count += 1
        logger.info(f"Starting Voice Screening Session for call {self.call_id} from {self.caller_name}")
        
        await self._emit("SESSION_STARTED", {"status": "GREETING", "turn": self.turn_count})

        # Generate greeting turn
        turn_data = await self.ai_engine.generate_voice_screening_turn(
            caller_name=self.caller_name,
            category=self.category,
            user_context=self.user_context,
            conversation_history=[],
            turn_number=self.turn_count
        )

        greeting_text = turn_data.get("speech_text", "")
        self.transcript.append({
            "role": "assistant",
            "message": greeting_text,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

        # Synthesize audio
        audio_path = await self.voice_pipeline.synthesize(greeting_text, filename_prefix="greeting")
        if audio_path:
            self.audio_files.append(audio_path)
            if self.on_audio_ready:
                try:
                    self.on_audio_ready(audio_path)
                except Exception as e:
                    logger.warning(f"Error playing audio on device: {e}")

        await self._emit("AI_SPOKE", {
            "role": "assistant",
            "message": greeting_text,
            "audio_file": audio_path,
            "transcript": self.transcript
        })

        return greeting_text

    async def process_caller_response(self, caller_text: str) -> Dict[str, Any]:
        """
        Receives caller's spoken input (or simulated speech), assesses it,
        and generates the AI assistant's spoken reply.
        """
        if not self.is_active:
            return {"status": "ENDED", "reply": ""}

        self.turn_count += 1
        clean_text = caller_text.strip()
        self.transcript.append({
            "role": "caller",
            "message": clean_text,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

        await self._emit("CALLER_SPOKE", {
            "role": "caller",
            "message": clean_text,
            "turn": self.turn_count,
            "transcript": self.transcript
        })

        # Evaluate response with AI Engine
        turn_result = await self.ai_engine.generate_voice_screening_turn(
            caller_name=self.caller_name,
            category=self.category,
            user_context=self.user_context,
            conversation_history=self.transcript,
            turn_number=self.turn_count
        )

        reply_text = turn_result.get("speech_text", "Thank you. Your message has been noted.")
        should_end = turn_result.get("should_end_call", False) or (self.turn_count >= self.max_turns)

        self.transcript.append({
            "role": "assistant",
            "message": reply_text,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

        # Synthesize speech
        audio_path = await self.voice_pipeline.synthesize(reply_text, filename_prefix=f"turn_{self.turn_count}")
        if audio_path:
            self.audio_files.append(audio_path)

        await self._emit("AI_SPOKE", {
            "role": "assistant",
            "message": reply_text,
            "audio_file": audio_path,
            "transcript": self.transcript,
            "should_end": should_end
        })

        if should_end:
            await self.end_session()

        return {
            "reply_text": reply_text,
            "should_end": should_end,
            "audio_file": audio_path
        }

    async def end_session(self) -> Dict[str, Any]:
        """
        Concludes screening session:
        Generates executive summary, saves complete record to DB, and emits completion event.
        """
        self.is_active = False
        duration = int((datetime.now(timezone.utc) - self.start_time).total_seconds())

        # Generate summary via AI Engine
        summary = await self.ai_engine.summarize_call(
            caller_name=self.caller_name,
            caller_number=self.caller_number,
            transcript=self.transcript
        )

        # Check urgency
        has_urgent_keywords = any("urgent" in t["message"].lower() or "emergency" in t["message"].lower() for t in self.transcript if t["role"] == "caller")
        urgency_score = 8 if has_urgent_keywords else 4
        urgency_label = "HIGH" if urgency_score >= 7 else "MEDIUM"

        session_result = {
            "call_id": self.call_id,
            "duration": duration,
            "transcript": self.transcript,
            "ai_summary": summary,
            "urgency_score": urgency_score,
            "urgency_label": urgency_label,
            "audio_file": self.audio_files[-1] if self.audio_files else None
        }

        # Update CallLog in database
        from backend.database import db
        db.update_call(self.call_id, {
            "duration_seconds": duration,
            "transcript": self.transcript,
            "ai_summary": summary,
            "urgency_score": urgency_score,
            "urgency_label": urgency_label,
            "audio_file": session_result["audio_file"],
            "status": "SCREENED"
        })

        await self._emit("SESSION_COMPLETED", session_result)
        logger.info(f"Screening session completed for {self.caller_name}. Summary: {summary[:60]}...")
        return session_result
