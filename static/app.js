const recordBtn = document.getElementById("recordBtn");
const recordLabel = document.getElementById("recordLabel");
const audioInput = document.getElementById("audioInput");
const processBtn = document.getElementById("processBtn");
const selectedFile = document.getElementById("selectedFile");
const recordingStatus = document.getElementById("recordingStatus");
const errorBox = document.getElementById("errorBox");
const loading = document.getElementById("loading");
const loadingText = document.getElementById("loadingText");
const results = document.getElementById("results");

let currentAudio = null;
let recorder = null;
let chunks = [];
let stream = null;

function showError(message) {
  errorBox.textContent = message;
  errorBox.classList.remove("hidden");
}

function clearError() {
  errorBox.textContent = "";
  errorBox.classList.add("hidden");
}

function setSelectedFile(file) {
  currentAudio = file;
  selectedFile.textContent = `${file.name} · ${(file.size / 1024 / 1024).toFixed(2)} MB`;
  selectedFile.classList.remove("hidden");
  processBtn.disabled = false;
}

function preferredMimeType() {
  const options = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"];
  return options.find((type) => MediaRecorder.isTypeSupported(type)) || "";
}

recordBtn.addEventListener("click", async () => {
  clearError();

  if (recorder && recorder.state === "recording") {
    recorder.stop();
    return;
  }

  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    showError("This browser cannot record audio here. Upload an audio file instead.");
    return;
  }

  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    chunks = [];
    const mimeType = preferredMimeType();
    recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);

    recorder.addEventListener("dataavailable", (event) => {
      if (event.data.size > 0) chunks.push(event.data);
    });

    recorder.addEventListener("stop", () => {
      const blob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
      const ext = (recorder.mimeType || "").includes("ogg") ? "ogg" : "webm";
      setSelectedFile(new File([blob], `voicenote-${Date.now()}.${ext}`, { type: blob.type }));
      recordingStatus.textContent = "Recording ready. Listen back mentally, then analyze it.";
      recordingStatus.classList.remove("hidden");
      stream?.getTracks().forEach((track) => track.stop());
      stream = null;
    });

    recorder.start();
    recordBtn.classList.add("recording");
    recordLabel.textContent = "Stop recording";
    recordingStatus.textContent = "Recording… click again when you're done.";
    recordingStatus.classList.remove("hidden");
  } catch (error) {
    showError(`Microphone access failed: ${error.message}`);
  }
});

audioInput.addEventListener("change", () => {
  clearError();
  const file = audioInput.files?.[0];
  if (file) setSelectedFile(file);
});

processBtn.addEventListener("click", async () => {
  if (!currentAudio) return;
  clearError();
  results.classList.add("hidden");
  loading.classList.remove("hidden");
  processBtn.disabled = true;
  loadingText.textContent = "Whisper is transcribing your voice note…";

  const formData = new FormData();
  formData.append("audio", currentAudio);

  try {
    const response = await fetch("/api/process", { method: "POST", body: formData });
    loadingText.textContent = "Qwen3 is turning the transcript into actions…";
    const data = await response.json();

    if (!response.ok) throw new Error(data.error || "Something went wrong.");
    renderResults(data);
    results.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    showError(error.message);
  } finally {
    loading.classList.add("hidden");
    processBtn.disabled = false;
  }
});

function renderResults(data) {
  document.getElementById("summaryText").textContent = data.analysis.summary || "No summary was generated.";
  document.getElementById("transcriptText").textContent = data.transcript;
  document.getElementById("languageLabel").textContent = data.language || "auto-detected";
  document.getElementById("modelBadge").textContent = `${data.model.speech} + ${data.model.llm} · local`;

  const taskList = document.getElementById("taskList");
  taskList.innerHTML = "";
  const tasks = Array.isArray(data.analysis.tasks) ? data.analysis.tasks : [];

  if (!tasks.length) {
    taskList.innerHTML = '<div class="task"><span class="task-title">No clear action items detected.</span></div>';
  } else {
    tasks.forEach((task) => {
      const el = document.createElement("div");
      el.className = "task";
      const priority = ["high", "medium", "low"].includes(task.priority) ? task.priority : "low";
      const deadline = task.deadline ? `<div class="deadline">Deadline: ${escapeHtml(task.deadline)}</div>` : "";
      el.innerHTML = `
        <div class="task-top">
          <span class="task-title">${escapeHtml(task.title || "Untitled task")}</span>
          <span class="priority ${priority}">${priority}</span>
        </div>
        ${deadline}
      `;
      taskList.appendChild(el);
    });
  }

  const notesList = document.getElementById("notesList");
  notesList.innerHTML = "";
  const notes = Array.isArray(data.analysis.notes) ? data.analysis.notes : [];
  if (!notes.length) {
    notesList.innerHTML = "<li>No additional details detected.</li>";
  } else {
    notes.forEach((note) => {
      const li = document.createElement("li");
      li.textContent = note;
      notesList.appendChild(li);
    });
  }

  results.classList.remove("hidden");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
