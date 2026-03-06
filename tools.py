import logging
from livekit.agents import function_tool, RunContext
import requests
from langchain_community.tools import DuckDuckGoSearchRun
import os
from datetime import datetime
import time
import smtplib
from email.mime.multipart import MIMEMultipart  
from email.mime.text import MIMEText
from typing import Optional
from serpapi import GoogleSearch

from pathlib import Path
import subprocess
import csv
from rapidfuzz import process
import webbrowser
from urllib.parse import quote_plus
import yt_dlp
from dotenv import load_dotenv

from memory_store import forget_fact, recall_facts, remember_fact

load_dotenv()

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"


def _extract_openrouter_text(payload: dict) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""

    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()

    # Some OpenAI-compatible providers return list content blocks.
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts).strip()

    return ""


@function_tool()
async def openrouter_chat(context: RunContext, prompt: str, system_prompt: Optional[str] = None) -> str:
    """
    Generate a response using OpenRouter chat completions.
    Use this for general conversation and knowledge Q&A when OPENROUTER_API_KEY is configured.
    """
    api_key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
    if not api_key:
        return "OpenRouter is not configured. Please set OPENROUTER_API_KEY in the environment."

    model = (os.getenv("OPENROUTER_MODEL") or "z-ai/glm-4.5-air:free").strip()
    app_title = (os.getenv("OPENROUTER_APP_NAME") or "Voice Assistant").strip()
    referer = (os.getenv("OPENROUTER_SITE_URL") or "https://localhost").strip()

    user_prompt = (prompt or "").strip()
    if not user_prompt:
        return "Please provide a prompt for OpenRouter."

    messages = []
    if system_prompt and system_prompt.strip():
        messages.append({"role": "system", "content": system_prompt.strip()})
    messages.append({"role": "user", "content": user_prompt})

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": referer,
        "X-Title": app_title,
    }

    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.7,
    }

    try:
        response = requests.post(
            OPENROUTER_API_URL,
            headers=headers,
            json=body,
            timeout=45,
        )
    except requests.RequestException as exc:
        logging.error("OpenRouter request failed: %s", exc)
        return f"OpenRouter request failed: {exc}"

    if response.status_code >= 400:
        detail = ""
        try:
            detail = (response.json().get("error") or {}).get("message", "")
        except Exception:
            detail = response.text.strip()
        detail = detail or "unknown error"
        logging.error("OpenRouter error %s: %s", response.status_code, detail)
        return f"OpenRouter error {response.status_code}: {detail}"

    try:
        data = response.json()
    except ValueError:
        return "OpenRouter returned non-JSON response."

    text = _extract_openrouter_text(data)
    if not text:
        return "OpenRouter returned no text response."

    return text

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


@function_tool()
async def open_app(context: RunContext, app_query: str) -> str:
    """Open an installed application by name."""
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


@function_tool()
async def close_app(context: RunContext, app_query: str) -> str:
    """Close a running application by name."""
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


@function_tool()
async def remember(context: RunContext, fact: str) -> str:
    """Save a user fact or preference to persistent memory."""
    created, saved_fact = remember_fact(fact)
    if not saved_fact:
        return "Please tell me what to remember."
    if created:
        return f"Saved to memory: {saved_fact}."
    return f"I already had that in memory: {saved_fact}."


@function_tool()
async def recall_memory(context: RunContext, query: Optional[str] = None) -> str:
    """Recall saved memory, optionally filtered by a query."""
    memories = recall_facts(query, limit=5)
    if not memories:
        if query:
            return f"I do not have memory matching '{query}'."
        return "I do not have any saved memory yet."
    lines = [f"- {item.get('text', '')}" for item in memories]
    return "Here is what I remember:\n" + "\n".join(lines)


@function_tool()
async def forget_memory(context: RunContext, query: str) -> str:
    """Forget a saved memory item using fuzzy matching."""
    removed, removed_text = forget_fact(query)
    if removed:
        return f"I've forgotten: {removed_text}."
    return f"I could not find a memory matching '{query}'."



@function_tool()
async def get_weather(
    context: RunContext,  # type: ignore
    city: str) -> str:
    """
    Get the current weather for a given city.
    """
    try:
        response = requests.get(
            f"https://wttr.in/{city}?format=3")
        if response.status_code == 200:
            logging.info(f"Weather for {city}: {response.text.strip()}")
            return response.text.strip()   
        else:
            logging.error(f"Failed to get weather for {city}: {response.status_code}")
            return f"Could not retrieve weather for {city}."
    except Exception as e:
        logging.error(f"Error retrieving weather for {city}: {e}")
        return f"An error occurred while retrieving weather for {city}." 

@function_tool()
async def search_web(context: RunContext, query: str) -> str:
    """
    Search the web using Google SERP API and return top results.
    """
    try:
        params = {
            "engine": "google",
            "q": query,
            "api_key": os.getenv("SERPAPI_KEY"),
            "hl": "en",  # ensure results are in English
            "num": 5,    # number of results
        }
        search = GoogleSearch(params)
        results = search.get_dict()

        # Extract top organic results
        if "organic_results" in results:
            summaries = []
            for res in results["organic_results"][:3]:  # top 3
                title = res.get("title")
                snippet = res.get("snippet")
                link = res.get("link")
                summaries.append(f"- {title}: {snippet} ({link})")
            
            return "Here are the top search results:\n" + "\n".join(summaries)
        else:
            return "No results found."
    except Exception as e:
        return f"An error occurred while searching: {str(e)}"

@function_tool()    
async def send_email(
    context: RunContext,  # type: ignore
    to_email: str,
    subject: str,
    message: str,
    cc_email: Optional[str] = None
) -> str:
    """
    Send an email through Gmail.
    
    Args:
        to_email: Recipient email address
        subject: Email subject line
        message: Email body content
        cc_email: Optional CC email address
    """
    try:
        # Gmail SMTP configuration
        smtp_server = "smtp.gmail.com"
        smtp_port = 587
        
        # Get credentials from environment variables
        gmail_user = (
            os.getenv("GMAIL_USER")
            or os.getenv("EMAIL_USER")
            or ""
        ).strip()
        gmail_password = (
            os.getenv("GMAIL_APP_PASSWORD")
            or os.getenv("GMAIL_PASSWORD")
            or os.getenv("EMAIL_APP_PASSWORD")
            or ""
        ).strip()

        missing = []
        if not gmail_user:
            missing.append("GMAIL_USER")
        if not gmail_password:
            missing.append("GMAIL_APP_PASSWORD")
        if missing:
            logging.error("Missing email config: %s", ", ".join(missing))
            return (
                "Email sending failed: missing "
                + ", ".join(missing)
                + ". Add them to .env (Gmail requires an App Password)."
            )
        
        # Create message
        msg = MIMEMultipart()
        msg['From'] = gmail_user
        msg['To'] = to_email
        msg['Subject'] = subject
        
        # Add CC if provided
        recipients = [to_email]
        if cc_email:
            msg['Cc'] = cc_email
            recipients.append(cc_email)
        
        # Attach message body
        msg.attach(MIMEText(message, 'plain'))
        
        # Connect to Gmail SMTP server
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()  # Enable TLS encryption
        server.login(gmail_user, gmail_password)
        
        # Send email
        text = msg.as_string()
        server.sendmail(gmail_user, recipients, text)
        server.quit()
        
        logging.info(f"Email sent successfully to {to_email}")
        return f"Email sent successfully to {to_email}"
        
    except smtplib.SMTPAuthenticationError:
        logging.error("Gmail authentication failed")
        return "Email sending failed: Authentication error. Please check your Gmail credentials."
    except smtplib.SMTPException as e:
        logging.error(f"SMTP error occurred: {e}")
        return f"Email sending failed: SMTP error - {str(e)}"
    except Exception as e:
        logging.error(f"Error sending email: {e}")
        return f"An error occurred while sending email: {str(e)}"
    
import json

NOTES_FILE = "assistant_notes.txt"


def _load_notes():
    if not os.path.exists(NOTES_FILE):
        return []
    with open(NOTES_FILE, "r", encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip()]


def _save_notes(notes):
    with open(NOTES_FILE, "w", encoding="utf-8") as f:
        for note in notes:
            f.write(note + "\n")


@function_tool()
async def add_note(context: RunContext, note: str) -> str:
    """Add a new note."""
    notes = _load_notes()
    notes.append(note.strip())
    _save_notes(notes)
    return f"I've added this note: {note.strip()}"


@function_tool()
async def read_notes(context: RunContext) -> str:
    """Read all saved notes."""
    notes = _load_notes()
    if not notes:
        return "Your notes are empty."
    response = [f"Note {i+1}: {note}" for i, note in enumerate(notes)]
    return "\n".join(response)


@function_tool()
async def delete_note(context: RunContext, identifier: str) -> str:
    """
    Delete a note by number or matching text.
    Example: "delete note 2" or "delete note shopping".
    """
    notes = _load_notes()
    if not notes:
        return "No notes found."

    # If identifier is a number
    if identifier.isdigit():
        idx = int(identifier) - 1
        if 0 <= idx < len(notes):
            removed = notes.pop(idx)
            _save_notes(notes)
            return f"Deleted note {identifier}: {removed}"
        return f"No note found with number {identifier}."

    # Match by text (case-insensitive contains)
    matches = [n for n in notes if identifier.lower() in n.lower()]
    if matches:
        removed = matches[0]
        notes.remove(removed)
        _save_notes(notes)
        return f"Deleted note: {removed}"

    return f"No matching note found for '{identifier}'."

TODO_FILE = "todo_list.json"

def _load_todo_list():
    if not os.path.exists(TODO_FILE):
        return []
    with open(TODO_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []

def _save_todo_list(tasks):
    with open(TODO_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, indent=2)

@function_tool()
async def add_task(context: RunContext, description: str, deadline: Optional[str] = None) -> str:
    """Add a new task to the to-do list with optional deadline."""
    tasks = _load_todo_list()
    tasks.append({
        "description": description,
        "deadline": deadline,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })
    _save_todo_list(tasks)
    return f"I’ve added the task: '{description}'" + (f" with deadline '{deadline}'." if deadline else ".")


@function_tool()
async def read_tasks(context: RunContext) -> str:
    """Read all tasks from the to-do list."""
    tasks = _load_todo_list()
    if not tasks:
        return "Your to-do list is empty."
    response = [f"Task {i+1}: {t['description']}" + (f" (deadline: {t['deadline']})" if t.get("deadline") else "")
                for i, t in enumerate(tasks)]
    return "\n".join(response)


@function_tool()
async def complete_task(context: RunContext, identifier: str) -> str:
    """
    Complete a task by number or matching description.
    Example: '2' or 'buy milk'.
    """
    tasks = _load_todo_list()
    if not tasks:
        return "Your to-do list is empty."

    if identifier.isdigit():
        idx = int(identifier) - 1
        if 0 <= idx < len(tasks):
            removed = tasks.pop(idx)
            _save_todo_list(tasks)
            return f"Marked '{removed['description']}' as completed."
        return f"No task found with number {identifier}."

    match = process.extractOne(identifier.lower(), [t["description"].lower() for t in tasks])
    if match and match[1] >= 70:
        idx = [t["description"].lower() for t in tasks].index(match[0])
        removed = tasks.pop(idx)
        _save_todo_list(tasks)
        return f"Marked '{removed['description']}' as completed."
    return f"No matching task found for '{identifier}'."


# ==== Browser Control Tools ====

# Common website aliases for convenience
SITE_ALIASES = {
    "youtube": "youtube.com",
    "google": "google.com",
    "github": "github.com",
    "twitter": "twitter.com",
    "x": "x.com",
    "facebook": "facebook.com",
    "instagram": "instagram.com",
    "reddit": "reddit.com",
    "linkedin": "linkedin.com",
    "amazon": "amazon.com",
    "netflix": "netflix.com",
    "spotify": "spotify.com",
    "gmail": "mail.google.com",
    "chatgpt": "chat.openai.com",
    "claude": "claude.ai",
}


@function_tool()
async def open_website(context: RunContext, website: str) -> str:
    """
    Open a website in the default browser.
    Can handle full URLs (https://example.com) or just domain names (example.com).
    Also supports common aliases like 'youtube', 'github', etc.
    """
    try:
        website = website.lower().strip()
        
        # Check for common aliases
        if website in SITE_ALIASES:
            website = SITE_ALIASES[website]
        
        # Add https:// if no protocol specified
        if not website.startswith(("http://", "https://")):
            website = f"https://{website}"
        
        webbrowser.open(website)
        return f"Opening {website} in your browser."
    except Exception as e:
        return f"Failed to open website: {e}"


@function_tool()
async def search_in_browser(context: RunContext, query: str) -> str:
    """
    Search for something in the browser using Google.
    Use this when the user wants to search for information, tutorials, or any topic.
    Example: "search for how to use local LLM" or "look up python tutorials"
    """
    try:
        encoded_query = quote_plus(query)
        search_url = f"https://www.google.com/search?q={encoded_query}"
        webbrowser.open(search_url)
        return f"Searching Google for '{query}'."
    except Exception as e:
        return f"Failed to perform search: {e}"


@function_tool()
async def play_youtube(context: RunContext, query: str) -> str:
    """
    Search and play a video or song on YouTube.
    Use this when the user wants to play music, watch videos, or find content on YouTube.
    Example: "play baby by justin bieber" or "play lofi hip hop"
    """
    try:
        # Use yt-dlp to search YouTube
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
            'default_search': 'ytsearch1',  # Get first result
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(f"ytsearch1:{query}", download=False)
            
            if result and 'entries' in result and result['entries']:
                video = result['entries'][0]
                video_url = video.get('url') or f"https://www.youtube.com/watch?v={video.get('id')}"
                video_title = video.get('title', query)
                webbrowser.open(video_url)
                return f"Now playing '{video_title}' on YouTube."
            else:
                # Fallback to search results
                encoded_query = quote_plus(query)
                youtube_url = f"https://www.youtube.com/results?search_query={encoded_query}"
                webbrowser.open(youtube_url)
                return f"Couldn't find an exact match. Showing search results for '{query}'."
    except Exception as e:
        return f"Failed to play on YouTube: {e}"
