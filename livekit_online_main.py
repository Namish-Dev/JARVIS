import logging
import os
import time
from typing import Any

from dotenv import load_dotenv

from livekit import agents
from livekit.agents import Agent, AgentSession, RoomInputOptions
from livekit.plugins import noise_cancellation
from livekit.plugins import google
from memory_store import render_memory_context
from prompts import AGENT_INSTRUCTION, SESSION_INSTRUCTION
from tools import (
    add_note,
    add_task,
    close_app,
    complete_task,
    delete_note,
    forget_memory,
    get_weather,
    openrouter_chat,
    open_app,
    open_website,
    play_youtube,
    read_notes,
    read_tasks,
    recall_memory,
    remember,
    search_in_browser,
    search_web,
    send_email,
)

load_dotenv()


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_google_api_key() -> str:
    """Return a Gemini key for LiveKit realtime, accepting legacy env names too."""
    primary = (os.getenv("GOOGLE_API_KEY") or "").strip()
    if primary:
        return primary

    legacy = (os.getenv("Google_API_KEY") or os.getenv("GEMINI_API_KEY") or "").strip()
    if legacy:
        os.environ["GOOGLE_API_KEY"] = legacy
        return legacy

    return ""


TRACE_LOG_LEVEL = os.getenv("VOICE_TRACE_LEVEL", "INFO").upper()
TRACE_PARTIAL_TRANSCRIPTS = _env_bool("VOICE_TRACE_SHOW_PARTIAL", default=False)
TRACE_HISTORY_MESSAGES = _env_bool("VOICE_TRACE_SHOW_HISTORY", default=False)
TRACE_ASSISTANT_LAST_PARAGRAPH_ONLY = _env_bool(
    "VOICE_TRACE_ASSISTANT_LAST_PARAGRAPH_ONLY", default=True
)
SUPPRESS_DUPLICATE_TURNS = _env_bool("VOICE_SUPPRESS_DUPLICATE_TURNS", default=True)

TRACE_LOGGER = logging.getLogger("voice_trace")
TRACE_LOGGER.setLevel(getattr(logging, TRACE_LOG_LEVEL, logging.INFO))
TRACE_LOGGER.propagate = False
if not TRACE_LOGGER.handlers:
    trace_handler = logging.StreamHandler()
    trace_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    )
    TRACE_LOGGER.addHandler(trace_handler)


def _normalize_transcript(text: str) -> str:
    return " ".join(text.lower().split())


def _assistant_text_for_log(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        return cleaned

    if TRACE_ASSISTANT_LAST_PARAGRAPH_ONLY:
        paragraphs = [p.strip() for p in cleaned.split("\n\n") if p.strip()]
        if paragraphs:
            return paragraphs[-1]

    return cleaned


def _extract_message_text(item: Any) -> str:
    text_content = getattr(item, "text_content", "")
    if isinstance(text_content, str) and text_content.strip():
        return text_content.strip()

    chunks: list[str] = []
    for content in getattr(item, "content", []) or []:
        if isinstance(content, str):
            stripped = content.strip()
            if stripped:
                chunks.append(stripped)
            continue
        transcript = getattr(content, "transcript", "")
        if isinstance(transcript, str) and transcript.strip():
            chunks.append(transcript.strip())
    return " ".join(chunks).strip()


class TurnTracer:
    def __init__(self, duplicate_window_sec: float = 4.0) -> None:
        self._duplicate_window_sec = duplicate_window_sec
        self._last_user_normalized = ""
        self._last_user_at = 0.0
        self._turn_counter = 0

    def on_user_transcript(self, transcript: str) -> tuple[int, bool]:
        now = time.monotonic()
        normalized = _normalize_transcript(transcript)
        is_duplicate = (
            bool(normalized)
            and normalized == self._last_user_normalized
            and (now - self._last_user_at) <= self._duplicate_window_sec
        )
        if not is_duplicate:
            self._turn_counter += 1

        self._last_user_normalized = normalized
        self._last_user_at = now
        return self.current_turn(), is_duplicate

    def current_turn(self) -> int:
        return self._turn_counter if self._turn_counter > 0 else 0


def build_instructions() -> str:
    memory_context = render_memory_context(limit=8)
    openrouter_enabled = bool((os.getenv("OPENROUTER_API_KEY") or "").strip())

    openrouter_instruction = ""
    if openrouter_enabled:
        openrouter_instruction = (
            "\nOpenRouter mode is enabled.\n"
            "For general conversation, knowledge questions, coding help, writing, and explanations, call `openrouter_chat` first and use its response as your final answer.\n"
            "For device actions (apps, notes, tasks, browser, weather, email), use the dedicated action tool directly.\n"
        )

    return (
        AGENT_INSTRUCTION.strip()
        + "\n\n"
        + SESSION_INSTRUCTION.strip()
        + openrouter_instruction
        + "\nUse memory tools when users share stable preferences or personal details, and recall memory before answering when relevant.\n"
        + memory_context
        + "\nAlways rephrase tool outputs naturally and speak them aloud."
    )


class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=build_instructions(),
            tools=[
                openrouter_chat,
                get_weather,
                search_web,
                send_email,
                open_app,
                close_app,
                remember,
                recall_memory,
                forget_memory,
                add_note,
                read_notes,
                delete_note,
                add_task,
                read_tasks,
                complete_task,
                open_website,
                search_in_browser,
                play_youtube,
            ],
        )


async def entrypoint(ctx: agents.JobContext):
    backend = (os.getenv("VOICE_BACKEND") or "openrouter").strip().lower()
    if backend in {"openrouter", "zlm", "glm", "glm-openrouter"}:
        openrouter_model = (os.getenv("OPENROUTER_MODEL") or "z-ai/glm-4.5-air:free").strip()
        TRACE_LOGGER.info(
            "[SESSION] backend=openrouter model=%s (offline voice loop)",
            openrouter_model,
        )
        from offline_assistant_main import main as offline_main

        offline_main()
        return

    model_name = os.getenv(
        "GEMINI_REALTIME_MODEL", "gemini-2.5-flash-preview-native-audio-dialog"
    )
    google_api_key = _resolve_google_api_key()
    if not google_api_key:
        TRACE_LOGGER.warning(
            "GOOGLE_API_KEY missing. Falling back to OpenRouter offline voice mode."
        )
        from offline_assistant_main import main as offline_main

        offline_main()
        return

    session = AgentSession(
        turn_detection="realtime_llm",
        llm=google.beta.realtime.RealtimeModel(
            model=model_name,
            api_key=google_api_key,
            voice="Charon",
            temperature=0.8,
        ),
        # Reduce perceived lag before end-of-turn reply generation.
        min_endpointing_delay=0.35,
        max_endpointing_delay=2.0,
    )

    TRACE_LOGGER.info("[SESSION] realtime_model=%s", model_name)
    turn_tracer = TurnTracer()
    stop_phrases = {
        "stop",
        "stop it",
        "stop talking",
        "stop speaking",
        "be quiet",
        "shut up",
    }

    @session.on("user_input_transcribed")
    def on_user_input_transcribed(event: Any) -> None:
        transcript = (getattr(event, "transcript", "") or "").strip()
        if not transcript:
            return

        is_final = bool(getattr(event, "is_final", True))
        if not is_final:
            if TRACE_PARTIAL_TRANSCRIPTS:
                TRACE_LOGGER.debug("[USER][PARTIAL] %s", transcript)
            return

        turn_id, duplicate = turn_tracer.on_user_transcript(transcript)
        normalized = _normalize_transcript(transcript)

        if duplicate and SUPPRESS_DUPLICATE_TURNS:
            TRACE_LOGGER.warning(
                "[TURN %02d][USER] duplicate_of_previous=true suppressed=true text=%s",
                turn_id,
                transcript,
            )
            try:
                session.clear_user_turn()
            except Exception:
                pass
            return

        if normalized in stop_phrases:
            TRACE_LOGGER.info("[TURN %02d][CONTROL] stop detected -> interrupt", turn_id)
            try:
                session.clear_user_turn()
            except Exception:
                pass
            try:
                session.interrupt()
            except Exception as exc:
                TRACE_LOGGER.warning("[TURN %02d][CONTROL] interrupt failed: %s", turn_id, exc)
            return

        duplicate_tag = " duplicate_of_previous=true" if duplicate else ""
        language = getattr(event, "language", "unknown")
        speaker_id = getattr(event, "speaker_id", None)
        speaker_tag = f" speaker_id={speaker_id}" if speaker_id else ""
        TRACE_LOGGER.info(
            "[TURN %02d][USER] language=%s%s%s text=%s",
            turn_id,
            language,
            speaker_tag,
            duplicate_tag,
            transcript,
        )

    @session.on("conversation_item_added")
    def on_conversation_item_added(event: Any) -> None:
        item = getattr(event, "item", None)
        if item is None:
            return

        role = getattr(item, "role", "unknown")
        text = _extract_message_text(item)
        if not text:
            return

        interrupted = bool(getattr(item, "interrupted", False))
        interrupted_tag = " interrupted=true" if interrupted else ""
        if role == "assistant":
            spoken_text = _assistant_text_for_log(text)
            if not spoken_text:
                return

            TRACE_LOGGER.info(
                "[TURN %02d][LLM]%s text=%s",
                turn_tracer.current_turn(),
                interrupted_tag,
                spoken_text,
            )
        elif TRACE_HISTORY_MESSAGES:
            TRACE_LOGGER.debug("[HISTORY][%s]%s text=%s", role, interrupted_tag, text)

    @session.on("speech_created")
    def on_speech_created(event: Any) -> None:
        source = getattr(event, "source", "unknown")
        user_initiated = getattr(event, "user_initiated", False)
        TRACE_LOGGER.info(
            "[SPEECH_CREATED] source=%s user_initiated=%s",
            source,
            user_initiated,
        )

    @session.on("function_tools_executed")
    def on_function_tools_executed(event: Any) -> None:
        pairs: list[tuple[Any, Any]]
        zipped = getattr(event, "zipped", None)
        if callable(zipped):
            pairs = list(zipped())
        else:
            function_calls = getattr(event, "function_calls", []) or []
            function_outputs = getattr(event, "function_call_outputs", []) or []
            pairs = list(zip(function_calls, function_outputs))

        for function_call, function_output in pairs:
            tool_name = getattr(function_call, "name", "unknown_tool")
            arguments = getattr(function_call, "arguments", None)
            output = getattr(function_output, "output", function_output)
            TRACE_LOGGER.info(
                "[TOOL] name=%s args=%s output=%s",
                tool_name,
                arguments,
                output,
            )

    @session.on("agent_state_changed")
    def on_agent_state_changed(event: Any) -> None:
        old_state = getattr(event, "old_state", "unknown")
        new_state = getattr(event, "new_state", "unknown")
        TRACE_LOGGER.info("[AGENT_STATE] %s -> %s", old_state, new_state)

    @session.on("user_state_changed")
    def on_user_state_changed(event: Any) -> None:
        old_state = getattr(event, "old_state", "unknown")
        new_state = getattr(event, "new_state", "unknown")
        TRACE_LOGGER.info("[USER_STATE] %s -> %s", old_state, new_state)

    @session.on("error")
    def on_error(event: Any) -> None:
        error = getattr(event, "error", None)
        source = getattr(event, "source", None)
        source_name = type(source).__name__ if source is not None else "unknown"
        TRACE_LOGGER.error("[SESSION_ERROR] source=%s error=%s", source_name, error)

    @session.on("close")
    def on_close(event: Any) -> None:
        reason = getattr(event, "reason", "unknown")
        error = getattr(event, "error", None)
        TRACE_LOGGER.warning("[SESSION_CLOSE] reason=%s error=%s", reason, error)

    await session.start(
        room=ctx.room,
        agent=Assistant(),
        room_input_options=RoomInputOptions(
            # LiveKit Cloud enhanced noise cancellation
            # - If self-hosting, omit this parameter
            # - For telephony applications, use `BVCTelephony` for best results
            video_enabled=False,
            noise_cancellation=noise_cancellation.BVC(),
        ),
    )

    await session.generate_reply()


if __name__ == "__main__":
    backend = (os.getenv("VOICE_BACKEND") or "openrouter").strip().lower()
    if backend in {"openrouter", "zlm", "glm", "glm-openrouter"}:
        from offline_assistant_main import main as offline_main

        offline_main()
    else:
        agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint))
