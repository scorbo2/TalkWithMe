/**
 * theme.js — Theme selection, persistence, and application.
 */

function initTheme() {
    let storedTheme = "dark";
    try {
        const fromStorage = localStorage.getItem(THEME_STORAGE_KEY);
        if (fromStorage) {
            storedTheme = fromStorage;
        }
    } catch (err) {
        console.warn("Theme storage unavailable:", err);
    }
    applyTheme(storedTheme, false);
}

function applyTheme(theme, persist) {
    const allowedThemes = new Set(["dark", "light", "matrix", "blues"]);
    const normalizedTheme = allowedThemes.has(theme) ? theme : "dark";
    document.body.dataset.theme = normalizedTheme;
    themeSelectEl.value = normalizedTheme;

    if (persist) {
        try {
            localStorage.setItem(THEME_STORAGE_KEY, normalizedTheme);
        } catch (err) {
            console.warn("Failed to persist theme:", err);
        }
    }
}
