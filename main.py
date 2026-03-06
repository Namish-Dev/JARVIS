import socket
import subprocess
import sys

def is_online(host="8.8.8.8", port=53, timeout=3):
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except socket.error:
        return False

if __name__ == "__main__":
    print("Detecting internet connection...")
    if is_online():
        print("Internet detected. Launching online LLM voice assistant (Groq).")
        subprocess.run([sys.executable, "online_llm_main.py"])
    else:
        print("No internet. Launching offline voice assistant.")
        subprocess.run([sys.executable, "offline_assistant_main.py"])
