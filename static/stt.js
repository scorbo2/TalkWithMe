/**
 * stt.js — Speech-to-Text: microphone recording and transcription.
 */

function updateMicButtonUI() {
    micBtn.disabled = !sttAvailable;
}

async function toggleMicrophone() {
    if (mediaRecorder && mediaRecorder.state === "recording") {
        micBtn.disabled = true; // prevent re-entry until onstop finishes
        mediaRecorder.stop();
        return;
    }

    let stream;
    try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
        console.error("Microphone access denied:", err);
        appendErrorBubble("Microphone access was denied.");
        return;
    }

    recordedChunks = [];
    mediaRecorder = new MediaRecorder(stream);
    // Capture the actual MIME type the browser chose. Different browsers/platforms
    // may produce webm, ogg, mp4, or other containers.
    const audioMimeType = mediaRecorder.mimeType || "audio/webm";
    mediaRecorder.ondataavailable = (e) => {
        if (e.data.size > 0) recordedChunks.push(e.data);
    };

    mediaRecorder.onstop = async () => {
        // Stop all tracks to release the microphone
        stream.getTracks().forEach(t => t.stop());
        micBtn.classList.remove("recording");
        micBtn.disabled = true;

        const blob = new Blob(recordedChunks, { type: audioMimeType });

        let audio_base64 = "";
        try {
            audio_base64 = await new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.onerror = () => reject(reader.error);
                reader.onload = () => {
                    const result = String(reader.result || "");
                    resolve(result.split(",")[1] || "");
                };
                reader.readAsDataURL(blob);
            });
        } catch (err) {
            console.error("Failed to read recorded audio:", err);
            appendErrorBubble("Unable to read recorded audio.");
            updateMicButtonUI();
            return;
        }
        let sttFailed = false;
        try {
            const resp = await fetch("/api/stt", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ audio_base64, audio_mime_type: audioMimeType }),
            });

            if (!resp.ok) {
                console.error("STT request failed:", resp.status);
                appendErrorBubble("Unable to process STT data.");
                sttFailed = true;
                return;
            }

            const data = await resp.json();
            if (data.text) {
                // Append transcribed text (never replace existing content)
                const existing = inputEl.value;
                inputEl.value = existing ? existing + " " + data.text : data.text;
                inputEl.dispatchEvent(new Event("input")); // trigger auto-resize

                // Persist the recorded audio before sending the message.
                // pendingUserMessageId is set by sendMessage() right before the
                // message is sent, but we need it here *before* sendMessage().
                // So we generate it now if it's not already set.
                if (!pendingUserMessageId) {
                    pendingUserMessageId = crypto.randomUUID();
                }

                try {
                    const result = await uploadAudioBlob(currentChatRoom, pendingUserMessageId, blob, audioMimeType);
                    if (result && result.filename) {
                        // Inject a playback button into the live user bubble
                        addAudioButtonToUserMessage(pendingUserMessageId, result.filename);
                    }
                } catch (err) {
                    console.warn("Failed to persist STT audio:", err);
                }
                sendMessage();
            }
        } catch (err) {
            console.error("STT error:", err);
            appendErrorBubble("Unable to process STT data.");
            sttFailed = true;
        } finally {
            if (!sttFailed) micBtn.disabled = false;
        }
    };

    mediaRecorder.start();
    micBtn.classList.add("recording");
}
