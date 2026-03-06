"""
Online LLM Voice Assistant
  STT  : AssemblyAI Streaming (Universal-Streaming)
  LLM  : Groq API (LLaMA 3.3 70B by default)
  TTS  : pyttsx3 (local, offline)

No Vosk, no Whisper, no LiveKit. Just mic → AssemblyAI → Groq → speaker.
"""

import os
import sys
import time
import threading
import logging
from typing import Type
import assemblyai

import pyttsx3
import requests
from dotenv import load_dotenv

import assemblyai as aai
from assemblyai.streaming.v3 import (
    BeginEvent,
    StreamingClient,
    StreamingClientOptions,
    StreamingError,
    StreamingEvents,
    StreamingParameters,
    TerminationEvent,
    TurnEvent,
)

from memory_store import render_memory_context

load_dotenv()

# ===== CONFIG =====
ASSEMBLYAI_API_KEY = (os.getenv("ASSEMBLYAI_API_KEY") or "").strip()
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_API_KEY = (os.getenv("GROQ_API_KEY") or "").strip()
GROQ_MODEL = (os.getenv("GROQ_MODEL") or "llama-3.3-70b-versatile").strip()

WAKE_WORD = "jarvis"
MAX_HISTORY_TURNS = 10

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("online_llm")

# ===== SYSTEM PROMPT =====
SYSTEM_PROMPT = """You are Jarvis, a personal AI assistant inspired by the AI from Iron Man.
- Speak like a classy, witty butler. Be slightly sarcastic but always helpful.
- Keep answers concise — one to three sentences unless the user asks for detail.
- When acknowledging tasks, say things like "Will do, Sir", "Roger Boss", "Check!".
- You have access to real-time information. Be confident, charming, and precise.
- Current date/time context will be provided with each message.
"""


# ===== GROQ CHAT CLASS =====
class GroqChat:
    """Manages multi-turn conversation with Groq API."""

    def __init__(self):
        self.history: list[dict] = []

    def _build_system_message(self) -> str:
        now_str = time.strftime("%A, %B %d, %Y at %I:%M %p")
        memory_ctx = render_memory_context(limit=8)
        return (
            SYSTEM_PROMPT.strip()
            + f"\n\nCurrent date/time: {now_str}"
            + f"\n{memory_ctx}"
        )

    def ask(self, user_text: str) -> str:
        if not GROQ_API_KEY:
            return "Groq API key is not configured. Please set GROQ_API_KEY in your .env file."

        self.history.append({"role": "user", "content": user_text})

        # Trim history
        max_messages = MAX_HISTORY_TURNS * 2
        if len(self.history) > max_messages:
            self.history = self.history[-max_messages:]

        messages = [{"role": "system", "content": self._build_system_message()}]
        messages.extend(self.history)

        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        }
        body = {
            "model": GROQ_MODEL,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 1024,
        }

        try:
            response = requests.post(
                GROQ_API_URL, headers=headers, json=body, timeout=30,
            )
        except requests.RequestException as exc:
            return f"I couldn't reach the Groq servers: {exc}"

        if response.status_code >= 400:
            detail = ""
            try:
                detail = (response.json().get("error") or {}).get("message", "")
            except Exception:
                detail = response.text[:200]
            return f"Groq API error ({response.status_code}): {detail or 'unknown'}"

        try:
            data = response.json()
        except ValueError:
            return "Groq returned an invalid response."

        choices = data.get("choices") or []
        if not choices:
            return "Groq returned no response."

        assistant_text = (choices[0].get("message") or {}).get("content", "").strip()
        if not assistant_text:
            return "Groq returned an empty response."

        self.history.append({"role": "assistant", "content": assistant_text})
        return assistant_text


# ===== TTS =====
_tts_lock = threading.Lock()


def speak_text(text: str):
    """Speak text aloud using pyttsx3 (thread-safe)."""
    if not text:
        return
    print(f"\n  Jarvis: {text}\n")
    with _tts_lock:
        try:
            engine = pyttsx3.init()
            engine.setProperty("rate", 160)
            engine.say(text)
            engine.runAndWait()
        except Exception as e:
            logger.warning("TTS error: %s", e)


# ===== COMMAND ROUTING =====
def is_action_command(text: str) -> bool:
    """Check if the text looks like a local action command."""
    lower = text.lower().strip()
    action_prefixes = [
        "open ", "close ", "quit ",
        "make a note", "take a note", "note down",
        "read notes", "show notes", "open notes",
        "remind me to ", "todo list", "to do list", "my tasks", "read my tasks",
        "mark task ",
        "remember that ", "remember this ", "remember ",
        "what do you remember", "recall memory", "show memory", "list memory",
        "forget ",
        "rename yourself",
    ]
    action_keywords = [
        "what's your name", "what is your name", "who are you",
    ]
    for prefix in action_prefixes:
        if lower.startswith(prefix):
            return True
    for kw in action_keywords:
        if kw in lower:
            return True
    return False


def route_command(text: str, groq_chat: GroqChat) -> str:
    """Route: action commands → handle_command(), chat → Groq LLM."""
    if is_action_command(text):
        from offline_assistant_main import handle_command
        return handle_command(text)
    return groq_chat.ask(text)


# ===== MAIN ASSISTANT =====
class VoiceAssistant:
    """
    Continuously streams mic audio to AssemblyAI.
    Listens for the wake word "Jarvis" in transcripts.
    After the wake word, the NEXT complete turn is treated as the command.
    """

    def __init__(self):
        self.groq_chat = GroqChat()
        self.waiting_for_command = False
        self._active_timer: threading.Timer | None = None
        self._active_timeout = 15.0  # seconds of active listening after wake word
        self._running = True

    # --- AssemblyAI event handlers ---

    def on_begin(self, client: Type[StreamingClient], event: BeginEvent):
        logger.info("AssemblyAI session started: %s", event.id)

    def on_turn(self, client: Type[StreamingClient], event: TurnEvent):
        """Called for every transcription turn (partial or final)."""
        transcript = (event.transcript or "").strip()
        if not transcript:
            return

        is_final = event.end_of_turn

        # Show partial transcripts
        if not is_final:
            print(f"  [partial] {transcript}", end="\r")
            return

        # Final transcript
        print(f"  You: {transcript}")
        lower = transcript.lower()

        # --- Exit commands ---
        if lower in {"exit", "quit", "stop", "bye", "goodbye",
                      "exit jarvis", "goodbye jarvis", "quit jarvis"}:
            speak_text("Goodbye, Sir. Until next time.")
            self._running = False
            return

        # --- If we are in active listening, handle the command ---
        if self.waiting_for_command:
            # Cancel the timeout timer
            if self._active_timer:
                self._active_timer.cancel()

            # Check if this is just the wake word again (ignore)
            stripped = lower.replace(WAKE_WORD, "").strip(" ,.:!?")
            if not stripped:
                # They just said "Jarvis" again, reset timer
                self._reset_active_timer()
                return

            # Process the command
            self.waiting_for_command = False
            response = route_command(transcript, self.groq_chat)
            speak_text(response)
            return

        # --- Check for wake word ---
        if WAKE_WORD in lower:
            # Check if there's a command AFTER the wake word in the same turn
            wake_idx = lower.index(WAKE_WORD) + len(WAKE_WORD)
            after_wake = transcript[wake_idx:].strip(" ,.:!?")

            if after_wake and len(after_wake) > 2:
                # Command in same turn: "Jarvis, what's the weather?"
                print(f"  [command] {after_wake}")
                response = route_command(after_wake, self.groq_chat)
                speak_text(response)
            else:
                # Just the wake word — enter active listening
                self.waiting_for_command = True
                self._reset_active_timer()
                speak_text("I'm listening, Sir.")

    def _reset_active_timer(self):
        if self._active_timer:
            self._active_timer.cancel()
        self._active_timer = threading.Timer(
            self._active_timeout, self._active_timeout_expired
        )
        self._active_timer.daemon = True
        self._active_timer.start()

    def _active_timeout_expired(self):
        if self.waiting_for_command:
            self.waiting_for_command = False
            print("  [timeout] Active listening ended. Say 'Jarvis' to wake me.")

    def on_terminated(self, client: Type[StreamingClient], event: TerminationEvent):
        logger.info(
            "AssemblyAI session ended: %.1f seconds processed",
            event.audio_duration_seconds,
        )

    def on_error(self, client: Type[StreamingClient], error: StreamingError):
        logger.error("AssemblyAI error: %s", error)

    # --- Run ---

    def run(self):
        if not ASSEMBLYAI_API_KEY:
            print("ERROR: ASSEMBLYAI_API_KEY is not set in .env")
            print("Get a free key at https://www.assemblyai.com/dashboard/signup")
            sys.exit(1)
        if not GROQ_API_KEY:
            print("WARNING: GROQ_API_KEY is not set. LLM responses won't work.")

        print("=" * 58)
        print("  JARVIS — Online Voice Assistant")
        print("  STT: AssemblyAI  |  LLM: Groq  |  TTS: pyttsx3")
        print("=" * 58)
        print(f"  Model     : {GROQ_MODEL}")
        print(f"  Wake word : \"{WAKE_WORD}\"")
        print("  Say 'Jarvis' followed by your command.")
        print("  Say 'exit' or 'goodbye' to quit.\n")

        speak_text("Online mode activated. Jarvis at your service, Sir.")

        client = StreamingClient(
            StreamingClientOptions(
                api_key=ASSEMBLYAI_API_KEY,
                api_host="streaming.assemblyai.com",
            )
        )

        client.on(StreamingEvents.Begin, self.on_begin)
        client.on(StreamingEvents.Turn, self.on_turn)
        client.on(StreamingEvents.Termination, self.on_terminated)
        client.on(StreamingEvents.Error, self.on_error)

        client.connect(
            StreamingParameters(
                sample_rate=16000,
                format_turns=True,
            )
        )

        try:
            client.stream(
                aai.extras.MicrophoneStream(sample_rate=16000)
            )
        except KeyboardInterrupt:
            print("\nInterrupted by user.")
        finally:
            client.disconnect(terminate=True)
            print("Jarvis shutting down.")


def main():
    assistant = VoiceAssistant()
    assistant.run()


if __name__ == "__main__":
    main()
