import json
import os
import re
import uuid
from pathlib import Path

import requests
import whisper
from flask import Flask, jsonify, render_template, request
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {"wav", "mp3", "m4a", "mp4", "mpeg", "mpga", "webm", "ogg", "flac"}
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:4b")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

print(f"[VoxBuddy] Loading Whisper model: {WHISPER_MODEL}")
WHISPER = whisper.load_model(WHISPER_MODEL)
print("[VoxBuddy] Whisper ready.")


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def extract_json_object(text: str) -> dict:
    """Extract the first valid JSON object from an LLM response.

    Local models sometimes wrap JSON in markdown fences, add a brief sentence,
    or emit a Qwen thinking block. We only need the actual JSON object.
    """
    if not isinstance(text, str):
        raise json.JSONDecodeError("Model response was not text", str(text), 0)

    cleaned = text.strip()

    # Remove Qwen/DeepSeek-style thinking blocks when present.
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<think>.*$", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"</think>", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip()

    # Remove markdown fences without assuming the model used them correctly.
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    # Fast path: the whole response is already valid JSON.
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Robust path: locate the first JSON object and decode only that object,
    # ignoring any harmless text before/after it. raw_decode handles nested
    # braces correctly instead of using a greedy regex.
    decoder = json.JSONDecoder()
    for start, char in enumerate(cleaned):
        if char != "{":
            continue
        try:
            result, _end = decoder.raw_decode(cleaned[start:])
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            continue

    raise json.JSONDecodeError("No valid JSON object found in model response", cleaned, 0)


def call_ollama(transcript: str) -> dict:
    system_prompt = """
You are VoxBuddy, a private voice-note assistant.
Turn the user's transcript into concise, useful action items.
Return ONLY valid JSON with this exact structure:
{
  "summary": "one or two sentence summary",
  "tasks": [
    {
      "title": "short actionable task",
      "priority": "high|medium|low",
      "deadline": "string or null"
    }
  ],
  "notes": ["important detail 1", "important detail 2"]
}
Rules:
- Do not invent facts.
- Preserve names, dates, times, quantities, and places when stated.
- If no deadline is stated, use null.
- A task must be actionable; ordinary statements belong in notes.
- Keep task titles under 90 characters.
- Keep notes short.
""".strip()

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": transcript},
        ],
        "stream": False,
        "think": False,
        "format": "json",
        "options": {
            "temperature": 0.2,
        },
    }

    response = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=180)
    response.raise_for_status()
    data = response.json()
    content = data.get("message", {}).get("content", "")

    try:
        result = extract_json_object(content)
    except json.JSONDecodeError:
        # One cheap recovery attempt. This helps older/local model builds that
        # ignore the JSON format hint and prepend conversational text.
        repair_payload = {
            "model": OLLAMA_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return ONLY a single valid JSON object. No markdown, no code fences, "
                        "no explanation, and no <think> tags. Use exactly these keys: "
                        "summary (string), tasks (array of objects with title, priority, deadline), "
                        "notes (array of strings). priority must be high, medium, or low. deadline "
                        "must be a string or null."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Convert this transcript to that JSON structure:\n\n{transcript}",
                },
            ],
            "stream": False,
            "think": False,
            "format": "json",
            "options": {"temperature": 0},
        }
        repair_response = requests.post(
            f"{OLLAMA_URL}/api/chat", json=repair_payload, timeout=180
        )
        repair_response.raise_for_status()
        repair_data = repair_response.json()
        repaired_content = repair_data.get("message", {}).get("content", "")
        result = extract_json_object(repaired_content)

    if not isinstance(result, dict):
        raise ValueError("Ollama returned an unexpected result format.")

    result.setdefault("summary", "")
    result.setdefault("tasks", [])
    result.setdefault("notes", [])
    return result


def transcribe_audio(audio_path: Path) -> dict:
    result = WHISPER.transcribe(str(audio_path), fp16=False)
    return {
        "text": result.get("text", "").strip(),
        "language": result.get("language") or "unknown",
    }


@app.get("/")
def index():
    return render_template("index.html", max_upload_mb=MAX_UPLOAD_MB)


@app.get("/api/health")
def health():
    ollama_ok = False
    ollama_error = None
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        r.raise_for_status()
        ollama_ok = True
    except Exception as exc:  # noqa: BLE001
        ollama_error = str(exc)

    return jsonify(
        {
            "status": "ok",
            "ollama": ollama_ok,
            "ollama_error": ollama_error,
            "whisper_model": WHISPER_MODEL,
            "ollama_model": OLLAMA_MODEL,
        }
    )


@app.post("/api/process")
def process_audio():
    if "audio" not in request.files:
        return jsonify({"error": "No audio file was provided."}), 400

    file = request.files["audio"]
    if not file.filename:
        return jsonify({"error": "Please choose or record an audio file."}), 400

    if not allowed_file(file.filename):
        return jsonify({"error": "Unsupported audio format."}), 400

    original_name = secure_filename(file.filename)
    ext = Path(original_name).suffix.lower() or ".webm"
    temp_path = UPLOAD_DIR / f"{uuid.uuid4().hex}{ext}"
    file.save(temp_path)

    try:
        transcript = transcribe_audio(temp_path)
        if not transcript["text"]:
            return jsonify({"error": "Whisper could not detect any speech."}), 422

        analysis = call_ollama(transcript["text"])
        return jsonify(
            {
                "transcript": transcript["text"],
                "language": transcript["language"],
                "analysis": analysis,
                "model": {
                    "speech": WHISPER_MODEL,
                    "language": transcript["language"],
                    "llm": OLLAMA_MODEL,
                    "local": True,
                },
            }
        )
    except requests.RequestException:
        return jsonify(
            {
                "error": "Could not reach Ollama. Make sure Ollama is running and the Qwen model is downloaded.",
                "hint": "Run: ollama serve   and   ollama run qwen3:4b",
            }
        ), 503
    except json.JSONDecodeError:
        return jsonify({"error": "The local language model returned invalid JSON. Please try again."}), 502
    except Exception as exc:  # noqa: BLE001
        app.logger.exception("Processing failed")
        return jsonify({"error": f"Processing failed: {exc}"}), 500
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"error": f"Audio file is too large. Maximum size is {MAX_UPLOAD_MB} MB."}), 413


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
