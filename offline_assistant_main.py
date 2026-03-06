import queue
import threading
import time
import random
import sys
import os
import subprocess
import json
import csv
from datetime import datetime, timedelta
from pathlib import Path
from rapidfuzz import process
import pyttsx3
import whisper
import vosk
import pyaudio
import numpy as np
import re
import requests
from dotenv import load_dotenv

from memory_store import forget_fact, recall_facts, remember_fact

load_dotenv()

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"


def _extract_openrouter_text(payload):
    choices = payload.get("choices") or []
    if not choices:
        return ""

    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts).strip()

    return ""


def _ask_openrouter(user_prompt):
    api_key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
    if not api_key:
        return ""

    model = (os.getenv("OPENROUTER_MODEL") or "z-ai/glm-4.5-air:free").strip()
    app_title = (os.getenv("OPENROUTER_APP_NAME") or "Voice Assistant").strip()
    referer = (os.getenv("OPENROUTER_SITE_URL") or "https://localhost").strip()

    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are Jarvis, a concise voice assistant. "
                    "Answer clearly in one short sentence unless the user asks for detail."
                ),
            },
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.7,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": referer,
        "X-Title": app_title,
    }

    try:
        response = requests.post(
            OPENROUTER_API_URL,
            headers=headers,
            json=body,
            timeout=40,
        )
        if response.status_code >= 400:
            return ""
        data = response.json()
        return _extract_openrouter_text(data)
    except Exception:
        return ""

# ==== CONFIG ====
CONFIG_FILE = "assistant_config.json"


def load_config():
    """Load configuration from file, with defaults"""
    default_config = {
        "wake_words": ["jarvis"],
        "assistant_name": "Jarvis"
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
                # Merge with defaults in case new fields are added
                default_config.update(config)
        except (json.JSONDecodeError, IOError):
            pass
    return default_config


def save_config(config):
    """Save configuration to file"""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except IOError as e:
        print(f"Error saving config: {e}")


# Load initial configuration
config = load_config()
WAKE_WORDS = config["wake_words"]
ASSISTANT_NAME = config["assistant_name"]

ACTIVE_LISTENING_SECONDS = 10
VOSK_MODEL_PATH = r"E:\\ai_voice_assistant\\vosk-model-small-en-us-0.15"
NOTES_FILE = "assistant_notes.txt"
TODO_FILE = "todo_list.json"
SMALL_TALK_COUNT = 0
LAST_INTERACTION = time.time()
SPEECH_INTERRUPT = threading.Event()


# ==== TTS ====
def speak_text(text):
    if not text:
        return True
    print(f"Assistant: {text}")
    engine = pyttsx3.init()
    engine.setProperty('rate', 150)
    interrupted = {"value": False}

    def _on_started_word(_name, _location, _length):
        if SPEECH_INTERRUPT.is_set():
            interrupted["value"] = True
            engine.stop()

    callback_token = engine.connect('started-word', _on_started_word)
    engine.say(text)
    engine.runAndWait()
    try:
        engine.disconnect(callback_token)
    except Exception:
        pass
    return not interrupted["value"]


# ==== App Discovery ====
app_map = {}
start_menu_paths = [
    Path(os.environ["PROGRAMDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
]
for menu_path in start_menu_paths:
    if menu_path.exists():
        for shortcut in menu_path.rglob("*.lnk"):
            app_map[shortcut.stem.lower()] = str(shortcut)


APP_PROCESS_ALIASES = {
    "visual studio code": "code.exe",
    "vscode": "code.exe",
    "code": "code.exe",
    "google chrome": "chrome.exe",
    "chrome": "chrome.exe",
    "microsoft edge": "msedge.exe",
    "edge": "msedge.exe",
    "telegram": "telegram.exe",
    "whatsapp": "whatsapp.exe",
    "discord": "discord.exe",
    "notepad": "notepad.exe",
    "spotify": "spotify.exe",
}


DIRECT_APP_TARGETS = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "paint": "mspaint.exe",
    "cmd": "cmd.exe",
    "command prompt": "cmd.exe",
    "powershell": "powershell.exe",
    "terminal": "wt.exe",
    "windows terminal": "wt.exe",
    "task manager": "taskmgr.exe",
    "explorer": "explorer.exe",
    "file explorer": "explorer.exe",
    "settings": "ms-settings:",
    "control panel": "control.exe",
}


def _wait_for_process(expected_process, timeout_sec=5.0):
    if not expected_process:
        return True
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if expected_process in _get_running_process_names():
            return True
        time.sleep(0.2)
    return False


def _launch_startfile(target, expected_process=None):
    try:
        os.startfile(target)
    except Exception as e:
        return False, str(e)
    if expected_process and not _wait_for_process(expected_process):
        return False, f"{expected_process} did not appear to start."
    return True, ""


def _launch_cmd_start(app_query, expected_process=None):
    try:
        subprocess.Popen(
            ["cmd", "/c", "start", "", app_query],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        return False, str(e)
    if expected_process and not _wait_for_process(expected_process):
        return False, f"{expected_process} did not appear to start."
    return True, ""


def open_app(app_query):
    query = (app_query or "").lower().strip()
    if not query:
        return "Which app should I open?"

    direct_target = DIRECT_APP_TARGETS.get(query)
    if direct_target:
        ok, err = _launch_startfile(direct_target, APP_PROCESS_ALIASES.get(query))
        if ok:
            return f"Opening {query}..."
        return f"Failed to open {query}: {err}"

    if query in app_map:
        ok, err = _launch_startfile(app_map[query], APP_PROCESS_ALIASES.get(query))
        if ok:
            return f"Opening {query}..."
        return f"Tried opening {query}, but it did not start: {err}"

    if app_map:
        match_result = process.extractOne(query, app_map.keys())
        if match_result:
            match, score, _ = match_result
            if score >= 75:
                expected = APP_PROCESS_ALIASES.get(query) or APP_PROCESS_ALIASES.get(match)
                ok, err = _launch_startfile(app_map[match], expected)
                if ok:
                    return f"Opening {match}..."
                return f"Tried opening {match}, but it did not start: {err}"
            if score >= 60:
                return f"I found '{match}', but I'm not confident. Please say the exact app name."

    expected = APP_PROCESS_ALIASES.get(query)
    if expected or query.endswith(".exe"):
        ok, err = _launch_cmd_start(query, expected)
        if ok:
            return f"Opening {query}..."
        return f"Failed to open {query}: {err}"

    return f"No app match found for '{app_query}'."


def _get_running_process_names():
    try:
        result = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
        )
        if result.returncode != 0:
            return []
        names = []
        for row in csv.reader(result.stdout.splitlines()):
            if row:
                proc_name = row[0].strip().lower()
                if proc_name.endswith(".exe"):
                    names.append(proc_name)
        return names
    except Exception:
        return []


def close_app(app_query):
    query = app_query.lower().strip()
    if not query:
        return "Which app should I close?"

    running = _get_running_process_names()
    if not running:
        return "I couldn't read running applications right now."

    alias_exe = APP_PROCESS_ALIASES.get(query)
    if alias_exe and alias_exe in running:
        result = subprocess.run(
            ["taskkill", "/IM", alias_exe, "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
        )
        if result.returncode == 0:
            return f"Closed {query}."
        err = (result.stderr or result.stdout or "unknown error").strip()
        return f"Failed to close {query}: {err}"

    process_labels = {}
    for proc_name in running:
        base = proc_name[:-4]
        process_labels[base] = proc_name
        process_labels[proc_name] = proc_name

    match_result = process.extractOne(query, process_labels.keys())
    if not match_result or match_result[1] < 70:
        return f"No running app close match found for '{app_query}'."

    matched_label, _, _ = match_result
    target_exe = process_labels[matched_label]
    result = subprocess.run(
        ["taskkill", "/IM", target_exe, "/F"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        check=False,
    )
    if result.returncode == 0:
        return f"Closed {target_exe[:-4]}."
    err = (result.stderr or result.stdout or "unknown error").strip()
    return f"Failed to close {target_exe[:-4]}: {err}"


# ==== NOTES ====
def add_note(note):
    with open(NOTES_FILE, "a", encoding="utf-8") as f:
        f.write(note.strip() + "\n")


def read_notes():
    try:
        with open(NOTES_FILE, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
        if not lines:
            return "Your notes are empty."
        for i, note in enumerate(lines, start=1):
            if not speak_text(f"Note {i}: {note}"):
                return "Paused reading notes."
        return f"I've read {len(lines)} notes."
    except FileNotFoundError:
        return "No notes yet."


def open_notes_in_notepad():
    if not os.path.exists(NOTES_FILE):
        with open(NOTES_FILE, "w", encoding="utf-8"):
            pass
    subprocess.Popen(["notepad.exe", NOTES_FILE])
    return "Opening notes in Notepad."


# ==== TODO LIST ====
def load_todo_list():
    if not os.path.exists(TODO_FILE):
        return []
    with open(TODO_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []


def save_todo_list(tasks):
    with open(TODO_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, indent=2)


def add_task(description, deadline):
    tasks = load_todo_list()
    tasks.append({
        "description": description,
        "deadline": deadline,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })
    save_todo_list(tasks)


def read_tasks():
    tasks = load_todo_list()
    if not tasks:
        return "Your to-do list is empty."
    for idx, task in enumerate(tasks, start=1):
        deadline_text = f" (deadline: {task['deadline']})" if task.get("deadline") else ""
        if not speak_text(f"Task {idx}: {task['description']}{deadline_text}"):
            return "Paused reading your tasks."
    return f"You have {len(tasks)} task(s) in your to-do list."


def complete_task(identifier):
    tasks = load_todo_list()
    if not tasks:
        return "Your to-do list is empty."

    if identifier.isdigit():
        idx = int(identifier) - 1
        if 0 <= idx < len(tasks):
            removed = tasks.pop(idx)
            save_todo_list(tasks)
            return f"Marked '{removed['description']}' as completed."
        else:
            return f"No task found with number {identifier}."

    match = process.extractOne(identifier.lower(), [t["description"].lower() for t in tasks])
    if match and match[1] >= 70:
        idx = [t["description"].lower() for t in tasks].index(match)
        removed = tasks.pop(idx)
        save_todo_list(tasks)
        return f"Marked '{removed['description']}' as completed."
    return f"No matching task found for '{identifier}'."


# ==== OVERDUE CHECK ====
def check_overdue_tasks():
    tasks = load_todo_list()
    if not tasks:
        return []

    overdue = []
    now = datetime.now()

    for task in tasks:
        if task.get("deadline"):
            try:
                deadline_str = task["deadline"]
                # handle "today" and "tomorrow"
                if "today" in deadline_str.lower():
                    today_str = now.strftime("%Y-%m-%d")
                    deadline_dt = datetime.strptime(today_str + " " + deadline_str.split("today")[1].strip(), "%Y-%m-%d %I:%M%p")
                elif "tomorrow" in deadline_str.lower():
                    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
                    deadline_dt = datetime.strptime(tomorrow_str + " " + deadline_str.split("tomorrow")[1].strip(), "%Y-%m-%d %I:%M%p")
                else:
                    # Try common formats
                    deadline_formats = [
                        "%Y-%m-%d %H:%M",
                        "%Y-%m-%d %I:%M%p",
                        "%d/%m/%Y %H:%M",
                        "%d/%m/%Y %I:%M%p",
                        "%I:%M%p %d/%m/%Y"
                    ]
                    deadline_dt = None
                    for fmt in deadline_formats:
                        try:
                            deadline_dt = datetime.strptime(deadline_str, fmt)
                            break
                        except ValueError:
                            continue

                if deadline_dt and deadline_dt < now:
                    overdue.append(task["description"])
            except Exception:
                pass

    return overdue


# ==== UTIL & CONTEXT ====
def normalize_text(text):
    if not text:
        return ""
    s = text.strip()
    s = s.replace('\u2019', "'").replace('\u201c', '"').replace('\u201d', '"')
    s = ''.join(ch if ord(ch) < 128 else ' ' for ch in s)
    s = re.sub(r'\s+', ' ', s).strip()
    # Remove any wake word from the beginning of the command
    for wake_word in WAKE_WORDS:
        pattern = rf'^(hey\s+)?{re.escape(wake_word)}[,:]?\s+'
        s = re.sub(pattern, '', s, flags=re.I)
    s = s.strip(" \t\n\r.,!?;:-")
    return s


def is_low_quality_asr(s):
    if not s:
        return True
    if len(s.strip()) < 2:
        return True
    letters = sum(1 for ch in s if ch.isalpha())
    if letters / max(1, len(s)) < 0.3:
        return True
    return False


def get_season(now=None):
    m = (now or datetime.now()).month
    return "winter" if m in (12, 1, 2) else \
           "spring" if m in (3, 4, 5) else \
           "summer" if m in (6, 7, 8) else "autumn"


# ==== HANDLE COMMAND ====
def handle_command(command_text):
    global SMALL_TALK_COUNT, LAST_INTERACTION, WAKE_WORDS, ASSISTANT_NAME
    raw = command_text or ""
    cmd = normalize_text(raw)
    cmd_lower = cmd.lower()

    assistant_exit_patterns = [
        r"^(exit|quit|stop|bye|goodbye)(\s+assistant)?$",
        rf"^(bye|goodbye)\s+{re.escape(ASSISTANT_NAME.lower())}$",
    ]
    if any(re.fullmatch(pattern, cmd_lower) for pattern in assistant_exit_patterns):
        return "Goodbye!"

    if is_low_quality_asr(cmd):
        return "I didn't catch that. Could you repeat?"

    now = datetime.now()
    hour = now.hour
    time_of_day_label = (
        "morning" if 5 <= hour < 12 else
        "afternoon" if 12 <= hour < 17 else
        "evening" if 17 <= hour < 22 else "night"
    )
    season = get_season(now)

    # ---------- Rename Assistant ----------
    if any(phrase in cmd.lower() for phrase in ["rename yourself to", "rename yourself as", "call yourself", "your name is now"]):
        rename_patterns = [
            r"rename yourself to (.+)",
            r"rename yourself as (.+)",
            r"call yourself (.+)",
            r"your name is now (.+)"
        ]

        new_name = None
        for pattern in rename_patterns:
            match = re.search(pattern, cmd, re.IGNORECASE)
            if match:
                new_name = match.group(1).strip()
                break

        if new_name and len(new_name) > 0:
            new_name = re.sub(r'\s+(please|okay|ok|now|then)$', '', new_name, flags=re.IGNORECASE)
            new_name = new_name.strip()
            old_name = ASSISTANT_NAME
            ASSISTANT_NAME = new_name
            WAKE_WORDS = [new_name.lower()]

            config["assistant_name"] = new_name
            config["wake_words"] = WAKE_WORDS
            save_config(config)

            return f"Great! I've renamed myself to {new_name}. You can now call me by saying '{new_name}'."

        return "What would you like to rename me to?"

    # Check current name
    if any(phrase in cmd.lower() for phrase in ["what's your name", "what is your name", "what are you called", "who are you"]):
        return f"My name is {ASSISTANT_NAME}. You can call me by saying '{ASSISTANT_NAME}'."

    # ---------- Notes ----------
    if any(p in cmd.lower() for p in ["make a note", "take a note", "note down"]):
        cmd_lower = cmd.lower()
        note = ""
        for phrase in ["make a note", "take a note", "note down"]:
            if phrase in cmd_lower:
                start_index = cmd_lower.index(phrase) + len(phrase)
                note = cmd[start_index:].strip()
                break
        if note:
            add_note(note)
            return f"I've added this note: {note}"
        else:
            return "What would you like me to note down?"

    if any(p in cmd.lower() for p in ["read notes", "show notes", "show my notes","read my notes"]):
        return read_notes()

    if "open notes" in cmd.lower():
        return open_notes_in_notepad()

    # ---------- To-do ----------
    if cmd.lower().startswith("remind me to "):
        content = cmd[13:].strip()
        deadline_match = re.search(r"deadline\s+(.*)", content, re.IGNORECASE)
        if deadline_match:
            description = content[:deadline_match.start()].strip()
            deadline = deadline_match.group(1).strip()
        else:
            description = content
            deadline = ""
        if description:
            add_task(description, deadline)
            return f"I've added the task: '{description}' with deadline '{deadline}'."
        else:
            return "What should I remind you about?"

    if any(p in cmd.lower() for p in ["todo list", "to do list", "my tasks", "read my tasks"]):
        return read_tasks()

    if cmd.lower().startswith("mark task "):
        match_num = re.search(r"mark task (\d+)", cmd, re.IGNORECASE)
        if match_num:
            return complete_task(match_num.group(1))
        else:
            match_desc = re.search(r"mark task (.+?) as completed", cmd, re.IGNORECASE)
            if match_desc:
                return complete_task(match_desc.group(1))
            else:
                return "Please specify the task number or description to mark as completed."

    # ---------- Memory ----------
    if cmd_lower.startswith("remember that "):
        fact = cmd[14:].strip()
        if not fact:
            return "What should I remember?"
        created, saved_fact = remember_fact(fact)
        if created:
            return f"Got it. I will remember that {saved_fact}."
        return f"I already remember that {saved_fact}."

    if cmd_lower.startswith("remember this "):
        fact = cmd[14:].strip()
        if not fact:
            return "What should I remember?"
        created, saved_fact = remember_fact(fact)
        if created:
            return f"Saved to memory: {saved_fact}."
        return f"I already had that in memory: {saved_fact}."

    if cmd_lower.startswith("remember ") and not cmd_lower.startswith("remember me to "):
        fact = cmd[9:].strip()
        if fact:
            created, saved_fact = remember_fact(fact)
            if created:
                return f"Saved to memory: {saved_fact}."
            return f"I already remember that {saved_fact}."

    if cmd_lower.startswith("what do you remember about "):
        query = cmd[27:].strip()
        memories = recall_facts(query, limit=5)
        if not memories:
            return f"I do not have any saved memory about {query}."
        lines = [f"- {item.get('text', '')}" for item in memories]
        return "Here is what I remember:\n" + "\n".join(lines)

    if cmd_lower in {"what do you remember", "recall memory", "show memory", "list memory"}:
        memories = recall_facts(limit=5)
        if not memories:
            return "I do not have any saved memory yet."
        lines = [f"- {item.get('text', '')}" for item in memories]
        return "Here is what I remember:\n" + "\n".join(lines)

    if cmd_lower.startswith("forget "):
        target = cmd[7:].strip()
        if not target:
            return "Tell me what to forget."
        removed, removed_text = forget_fact(target)
        if removed:
            return f"I've forgotten: {removed_text}."
        return f"I could not find a memory matching '{target}'."

    # ---------- Apps ----------
    if cmd_lower.startswith("open "):
        return open_app(cmd[5:].strip())
    if cmd_lower.startswith("close "):
        return close_app(cmd[6:].strip())
    if cmd_lower.startswith("quit "):
        target = cmd[5:].strip()
        if target.lower() in {"assistant", ASSISTANT_NAME.lower()}:
            return "Goodbye!"
        return close_app(target)

    # ---------- Small talk ----------
    if any(p in cmd.lower() for p in ["how are you", "how're you", "what's up", "what is up"]):
        SMALL_TALK_COUNT += 1
        LAST_INTERACTION = time.time()
        base = [
            f"I'm doing {random.choice(['great', 'well', 'fantastic'])}, thanks for asking!",
            f"I'm feeling {'energetic' if time_of_day_label == 'morning' else 'chill' if time_of_day_label == 'night' else 'focused'}, and ready to assist.",
            f"Honestly, it's a {season} {time_of_day_label}—{'a bit chilly' if season == 'winter' else 'quite nice' if season == 'spring' else 'rather warm' if season == 'summer' else 'a crisp autumn day'}, or so I imagine!"
        ]
        if SMALL_TALK_COUNT > 2:
            base.append("You really check in on me often — I appreciate it!")
        return random.choice(base) + " How about you?"

    if any(p in cmd.lower() for p in ["how is your day", "how's your day"]):
        SMALL_TALK_COUNT += 1
        LAST_INTERACTION = time.time()
        details = [
            f"It's been a productive {time_of_day_label} so far.",
            f"Just another {season} {time_of_day_label} in the digital realm.",
            "Every day’s a good day to learn something new."
        ]
        if time_of_day_label == "morning":
            details.append("A bright start! At least, I imagine so.")
        elif time_of_day_label == "evening":
            details.append("Evenings feel calm — perfect for conversations.")
        return random.choice(details) + " How has your day been?"

    if any(p in cmd.lower() for p in ["i am", "i'm", "my day is", "feeling"]):
        SMALL_TALK_COUNT += 1
        LAST_INTERACTION = time.time()
        responses = [
            "I hear you. Want me to tell you a joke?",
            "Got it. Maybe I can help make your day better."
        ]
        followups = [
            "Want me to note something down for you?",
            "Would you like to hear a joke?",
            "Shall I suggest something interesting?"
        ]
        return random.choice(responses) + " " + random.choice(followups)

    # ---------- Time ----------
    if "time" in cmd.lower():
        return f"The time is {now.strftime('%I:%M %p')}."

    # ---------- Fallback ----------
    openrouter_reply = _ask_openrouter(raw)
    if openrouter_reply:
        return openrouter_reply
    return f"I'm still learning: {raw}"


# ==== WAKE WORD THREAD ====
class WakeWordDetector(threading.Thread):
    def __init__(self, model_path, wake_words, queue_out):
        super().__init__(daemon=True)
        self.queue_out = queue_out
        self.wake_words = wake_words
        self.running = True
        print("Loading Vosk model...")
        self.model = vosk.Model(model_path)
        self.rec = vosk.KaldiRecognizer(self.model, 16000)
        self.pa = pyaudio.PyAudio()
        self.stream = self.pa.open(format=pyaudio.paInt16, channels=1, rate=16000,
                                   input=True, frames_per_buffer=8000)
        self.stream.start_stream()

    def update_wake_words(self, new_wake_words):
        """Update the wake words dynamically"""
        self.wake_words = new_wake_words
        print(f"Wake words updated to: {new_wake_words}")

    def run(self):
        while self.running:
            data = self.stream.read(4000, exception_on_overflow=False)
            if self.rec.AcceptWaveform(data):
                text = json.loads(self.rec.Result()).get("text", "")
                if any(phrase in text.lower() for phrase in ["stop talking", "stop speaking", "be quiet", "shut up"]):
                    SPEECH_INTERRUPT.set()
                    continue
                for w in self.wake_words:
                    if w in text.lower():
                        print(f"Wake word '{w}' detected!")
                        SPEECH_INTERRUPT.set()
                        self.queue_out.put("wake")
                        time.sleep(2)

    def stop(self):
        self.running = False
        self.stream.stop_stream()
        self.stream.close()
        self.pa.terminate()


# ==== WHISPER LISTEN ====
def listen_command_with_whisper(model, duration=6):
    p = pyaudio.PyAudio()
    stream = p.open(format=pyaudio.paFloat32, channels=1, rate=16000,
                    input=True, frames_per_buffer=4096)
    frames = []
    for _ in range(int(16000 / 4096 * duration)):
        frames.append(np.frombuffer(stream.read(4096), np.float32))
    stream.stop_stream()
    stream.close()
    p.terminate()
    audio = np.hstack(frames)
    res = model.transcribe(audio, language="en")
    text = res.get("text", "").strip()
    print(f"You said: {text}")
    return text


# ==== MAIN ====
def main():
    print(f"Starting {ASSISTANT_NAME} voice assistant...")
    print(f"Wake word: {', '.join(WAKE_WORDS)}")
    print("Say the wake word to activate, then speak your command.")
    print("You can say 'rename yourself to [name]' to change my name.")
    print("Say 'exit' or 'goodbye' to quit.\n")

    command_queue = queue.Queue()
    wake_detector = WakeWordDetector(VOSK_MODEL_PATH, WAKE_WORDS, command_queue)
    wake_detector.start()
    whisper_model = whisper.load_model("small")
    active = False
    end_time = 0

    # Create a function to handle commands with access to wake_detector
    def handle_command_with_wake_detector(command_text):
        global WAKE_WORDS, ASSISTANT_NAME
        response = handle_command(command_text)
        # Check if the wake words were updated (rename command was executed)
        if WAKE_WORDS != wake_detector.wake_words:
            wake_detector.update_wake_words(WAKE_WORDS)
        return response

    try:
        while True:
            if active and time.time() > end_time:
                active = False
                print("Active listening ended. Waiting for wake word...")

            try:
                if command_queue.get_nowait() == "wake":
                    SPEECH_INTERRUPT.clear()
                    if not active:
                        overdue_tasks = check_overdue_tasks()
                        if overdue_tasks:
                            speak_text("You have overdue tasks:")
                            for task in overdue_tasks:
                                speak_text(task)
                        else:
                            speak_text("Welcome back Sir. What are you planning on doing today?")
                        active = True
                    end_time = time.time() + ACTIVE_LISTENING_SECONDS
            except queue.Empty:
                pass

            if active:
                cmd = listen_command_with_whisper(whisper_model, 6)
                if cmd:
                    resp = handle_command_with_wake_detector(cmd)
                    speak_text(resp)
                    end_time = time.time() + ACTIVE_LISTENING_SECONDS
                    if "goodbye" in resp.lower() or "exit" in cmd.lower():
                        print("Exiting...")
                        break
                else:
                    print("No command detected.")
            else:
                time.sleep(0.05)
    finally:
        wake_detector.stop()


if __name__ == "__main__":
    main()
