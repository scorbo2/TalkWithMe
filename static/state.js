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
let maxPersonaReplies = 1;
let maxTurnsForContext = 6;
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
// UUID issued by the server in the "start" event; stamped onto TTS items at enqueue time
let currentAssistantMessageId = null;
let currentAssistantRow = null; // The active assistant bubble row (updated on each "start" event)

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
const pfAvatarImage = document.getElementById("pf-avatar-image");      // <input type=file>
const pfAvatarPreview = document.getElementById("pf-avatar-preview");
const pfAvatarRemoveBtn = document.getElementById("pf-avatar-remove");
const pfReferenceAudio = document.getElementById("pf-reference-audio"); // <input type=file>
const pfAudioStatus = document.getElementById("pf-audio-status");
const pfAudioPlayBtn = document.getElementById("pf-audio-play");
const pfAudioRemoveBtn = document.getElementById("pf-audio-remove");
const pfReferenceAudioTx = document.getElementById("pf-reference-audio-transcript");
const pfAllowToolCalls = document.getElementById("pf-allow-tool-calls");
const pfMemorySize = document.getElementById("pf-memory-size");
const pfMemoriesClearBtn = document.getElementById("pf-memories-clear");

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
// Reconnect button beside the Base URL (plan M4.1): re-probe the URL
// currently in the field (which may be an unsaved edit) and re-render the
// dynamic parameter section in place — no save + reopen required.
const sfTtsCapRefreshBtn = document.getElementById("sf-tts-cap-refresh");
// Dynamic TTS section (TTS generification, plan M4): containers filled by
// renderTtsInfo() / renderTtsParameters() from the engine's /capabilities doc.
const sfTtsInfo = document.getElementById("sf-tts-info");
const sfTtsParams = document.getElementById("sf-tts-params");
const sfTtsTimeout = document.getElementById("sf-tts-timeout");
const sfTtsStreaming = document.getElementById("sf-tts-streaming");
const sfTtsServerType = document.getElementById("sf-tts-server-type");

// Settings form fields — STT
const sfSttEnabled = document.getElementById("sf-stt-enabled");
const sfSttFields = document.getElementById("sf-stt-fields");
const sfSttBaseUrl = document.getElementById("sf-stt-base-url");
const sfSttTimeout = document.getElementById("sf-stt-timeout");

// General Settings Modal (used by gen-settings.js)
const genSettingsOverlay = document.getElementById("gen-settings-overlay");
const genSettingsForm = document.getElementById("gen-settings-form");
const genSettingsError = document.getElementById("gen-settings-error");
const gsfMaxPersonaReplies = document.getElementById("gsf-max-persona-replies");
const gsfPersonaNameMentions = document.getElementById("gsf-persona-name-mentions");
const gsfMaxTurnsForContext = document.getElementById("gsf-max-turns-for-context");
const gsfShowToolCalls = document.getElementById("gsf-show-tool-calls");
const gsfEnablePersonaMemories = document.getElementById("gsf-enable-persona-memories");
