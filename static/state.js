/**
 * state.js — Shared application state and DOM references.
 *
 * All modules read from and write to these globals. This avoids circular
 * dependencies between feature modules while keeping a single source of truth.
 */

/* ==========================================================================
   Application State
   ========================================================================== */

let personas = [];
let selectedPersona = null;
let personaNameMentionsEnabled = true;
let ttsEnabled = false;
let ttsAvailable = false;
let ttsStreaming = false;
let ttsServerType = "";
let sttAvailable = false;
let isStreaming = false;

// Chat room state
let currentChatRoom = "default";
let allChatRooms = [];
let roomPersonas = {};

// Microphone / STT state
let mediaRecorder = null;
let recordedChunks = [];

// Non-streaming: FIFO audio queue
const audioQueue = [];
let audioCtx = null;
let isPlayingAudio = false;

// Streaming TTS state
let sentenceBuffer = "";
let currentStreamingPersona = null;

// Streaming: decoupled fetch queue and decoded-buffer playback queue
const ttsRequestQueue = [];
const audioBufferQueue = [];
let isFetchingTTS = false;
let isPlayingAudioBuffer = false;

// Chat persistence — track message IDs for audio association
let pendingUserMessageId = null; // UUID generated before sending, used for STT audio
let currentAssistantMessageId = null; // UUID from server's "done" event, used for TTS audio
// Buffers audio fetched during streaming TTS (before message_id is known).
// Each entry: { audio_base64, mime_type }. Flushed when "done" event arrives.
let ttsAudioBuffer = [];

const THEME_STORAGE_KEY = "talkwithme_theme";

/* ==========================================================================
   DOM References — grouped by the module that uses them
   ========================================================================== */

// Main UI (used by chat.js, stt.js, app.js)
const messagesEl = document.getElementById("messages");
const inputEl = document.getElementById("message-input");
const sendBtn = document.getElementById("btn-send");
const micBtn = document.getElementById("btn-mic");
const newChatBtn = document.getElementById("btn-new-chat");
const ttsToggleBtn = document.getElementById("btn-tts-toggle");
const ttsIcon = document.getElementById("tts-icon");
const personaListEl = document.getElementById("persona-list");
const themeSelectEl = document.getElementById("theme-select");

// Chat room selector (used by chatrooms.js)
const chatRoomDropdown = document.getElementById("chat-room-dropdown");
const echoChamberToggle = document.getElementById("echo-chamber-toggle");
const btnAddPersona = document.getElementById("btn-add-persona");

// Persona Editor (used by persona.js)
const personaEditorOverlay = document.getElementById("persona-editor-overlay");
const peListView = document.getElementById("pe-list-view");
const peFormView = document.getElementById("pe-form-view");
const peListEl = document.getElementById("pe-list");
const peFormTitle = document.getElementById("pe-form-title");
const peFormError = document.getElementById("pe-form-error");
const peForm = document.getElementById("pe-form");
const peConfirmOverlay = document.getElementById("pe-confirm-overlay");
const peConfirmMsg = document.getElementById("pe-confirm-msg");

// Persona Editor form fields
const pfName = document.getElementById("pf-name");
const pfDescription = document.getElementById("pf-description");
const pfSystemPrompt = document.getElementById("pf-system-prompt");
const pfRouterHints = document.getElementById("pf-router-hints");
const pfAvatarColor = document.getElementById("pf-avatar-color");
const pfReferenceAudioLanguage = document.getElementById("pf-reference-audio-language");
const pfAvatarImage = document.getElementById("pf-avatar-image");
const pfReferenceAudio = document.getElementById("pf-reference-audio");
const pfReferenceAudioTx = document.getElementById("pf-reference-audio-transcript");

// Persona editor editing state
let peEditingName = null;

// Chat Rooms Editor (used by chatrooms.js)
const chatroomsOverlay = document.getElementById("chatrooms-overlay");
const crListEl = document.getElementById("cr-list");
const crFormError = document.getElementById("cr-form-error");
const crNewForm = document.getElementById("cr-new-form");
const crNameInput = document.getElementById("cr-name-input");
const crConfirmOverlay = document.getElementById("cr-confirm-overlay");
const crConfirmMsg = document.getElementById("cr-confirm-msg");

// Persona Picker (used by chatrooms.js)
const personaPickerOverlay = document.getElementById("persona-picker-overlay");
const ppListEl = document.getElementById("pp-list");
let ppSelectedNames = [];

// Settings Modal (used by settings.js)
const settingsOverlay = document.getElementById("settings-overlay");
const settingsForm = document.getElementById("settings-form");
const settingsError = document.getElementById("settings-error");

// Settings form fields — LLM
const sfLlmBaseUrl = document.getElementById("sf-llm-base-url");
const sfLlmModel = document.getElementById("sf-llm-model");
const sfLlmMaxTokens = document.getElementById("sf-llm-max-tokens");
const sfLlmTemperature = document.getElementById("sf-llm-temperature");

// Settings form fields — TTS
const sfTtsEnabled = document.getElementById("sf-tts-enabled");
const sfTtsFields = document.getElementById("sf-tts-fields");
const sfTtsBaseUrl = document.getElementById("sf-tts-base-url");
const sfTtsNumSteps = document.getElementById("sf-tts-num-steps");
const sfTtsGuidanceScale = document.getElementById("sf-tts-guidance-scale");
const sfTtsSeed = document.getElementById("sf-tts-seed");
const sfTtsTimeout = document.getElementById("sf-tts-timeout");
const sfTtsStreaming = document.getElementById("sf-tts-streaming");
const sfTtsServerType = document.getElementById("sf-tts-server-type");

// Settings form fields — STT
const sfSttEnabled = document.getElementById("sf-stt-enabled");
const sfSttFields = document.getElementById("sf-stt-fields");
const sfSttBaseUrl = document.getElementById("sf-stt-base-url");
const sfSttTimeout = document.getElementById("sf-stt-timeout");
