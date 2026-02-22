/**
 * Plex OAuth Authentication Module
 *
 * Handles Plex PIN-based OAuth authentication flow.
 * Opens a popup window for user to authenticate with plex.tv.
 */

class PlexAuth {
    constructor(options = {}) {
        this.pollInterval = options.pollInterval || 1000;
        this.pollTimeout = options.pollTimeout || 180000; // 3 minutes
        this.popup = null;
        this.pollTimer = null;
        this.onSuccess = options.onSuccess || (() => {});
        this.onError = options.onError || (() => {});
        this.onCancel = options.onCancel || (() => {});
    }

    /**
     * Request a new PIN from the server.
     * @returns {Promise<{id: number, code: string, auth_url: string}>}
     */
    async requestPin() {
        const response = await fetch('/api/plex/auth/pin', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': typeof getCsrfToken === 'function' ? getCsrfToken() : '',
            },
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.error || 'Failed to create PIN');
        }

        return response.json();
    }

    /**
     * Check if a PIN has been authenticated.
     * @param {number} pinId - The PIN ID to check
     * @returns {Promise<{authenticated: boolean, auth_token: string|null}>}
     */
    async checkPin(pinId) {
        const response = await fetch(`/api/plex/auth/pin/${pinId}`);

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.error || 'Failed to check PIN');
        }

        return response.json();
    }

    /**
     * Open the Plex authentication popup window.
     * @param {string} authUrl - The plex.tv auth URL
     * @returns {Window}
     */
    openAuthWindow(authUrl) {
        const width = 600;
        const height = 700;
        const left = (window.screen.width - width) / 2;
        const top = (window.screen.height - height) / 2;

        this.popup = window.open(
            authUrl,
            'PlexAuth',
            `width=${width},height=${height},left=${left},top=${top},` +
            'menubar=no,toolbar=no,location=no,status=no,resizable=yes,scrollbars=yes'
        );

        return this.popup;
    }

    /**
     * Poll for authentication token.
     * @param {number} pinId - The PIN ID to poll
     * @returns {Promise<string>} - The auth token when authenticated
     */
    pollForToken(pinId) {
        return new Promise((resolve, reject) => {
            const startTime = Date.now();

            const poll = async () => {
                // Check if timeout exceeded
                if (Date.now() - startTime > this.pollTimeout) {
                    this.cleanup();
                    reject(new Error('Authentication timed out'));
                    return;
                }

                // Check if popup was closed
                if (this.popup && this.popup.closed) {
                    this.cleanup();
                    reject(new Error('Authentication cancelled'));
                    return;
                }

                try {
                    const result = await this.checkPin(pinId);

                    if (result.authenticated) {
                        this.cleanup();
                        resolve(result.authenticated);
                        return;
                    }
                } catch (error) {
                    // Continue polling on network errors
                    console.warn('Poll error:', error);
                }

                // Continue polling
                this.pollTimer = setTimeout(poll, this.pollInterval);
            };

            poll();
        });
    }

    /**
     * Clean up popup and timers.
     */
    cleanup() {
        if (this.pollTimer) {
            clearTimeout(this.pollTimer);
            this.pollTimer = null;
        }
        if (this.popup && !this.popup.closed) {
            this.popup.close();
        }
        this.popup = null;
    }

    /**
     * Start the complete OAuth login flow.
     * @returns {Promise<string>} - The auth token when successful
     */
    async login() {
        try {
            // Step 1: Request PIN
            const pinData = await this.requestPin();
            console.log('PIN created:', pinData.id);

            // Step 2: Open popup
            this.openAuthWindow(pinData.auth_url);

            // Step 3: Poll for token
            const token = await this.pollForToken(pinData.id);
            console.log('Authentication successful');

            this.onSuccess(token);
            return token;

        } catch (error) {
            console.error('Plex auth error:', error);

            if (error.message === 'Authentication cancelled') {
                this.onCancel();
            } else {
                this.onError(error);
            }

            throw error;
        }
    }

    /**
     * Cancel the current authentication attempt.
     */
    cancel() {
        this.cleanup();
        this.onCancel();
    }
}

/**
 * Plex Server Manager
 *
 * Manages server discovery and selection.
 */
class PlexServerManager {
    /**
     * Fetch available Plex servers.
     * @returns {Promise<Array>}
     */
    async getServers() {
        const response = await fetch('/api/plex/servers');

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.error || 'Failed to get servers');
        }

        const data = await response.json();
        return data.servers || [];
    }

    /**
     * Fetch libraries from a Plex server.
     * @param {string} url - The Plex server URL (optional, uses saved)
     * @param {string} token - The Plex token (optional, uses saved)
     * @returns {Promise<Array>}
     */
    async getLibraries(url = null, token = null) {
        const params = new URLSearchParams();
        if (url) params.append('url', url);
        if (token) params.append('token', token);

        const queryString = params.toString();
        const endpoint = queryString ? `/api/plex/libraries?${queryString}` : '/api/plex/libraries';

        const response = await fetch(endpoint);

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.error || 'Failed to get libraries');
        }

        const data = await response.json();
        return data.libraries || [];
    }

    /**
     * Test connection to a Plex server.
     * @param {string} url - The Plex server URL
     * @param {string} token - The Plex token
     * @returns {Promise<{success: boolean, server_name: string|null, error: string|null}>}
     */
    async testConnection(url = null, token = null) {
        const response = await fetch('/api/plex/test', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': typeof getCsrfToken === 'function' ? getCsrfToken() : '',
            },
            body: JSON.stringify({ url, token }),
        });

        return response.json();
    }
}

/**
 * Settings Manager
 *
 * Manages application settings via API.
 */
class SettingsManager {
    /**
     * Get all settings.
     * @returns {Promise<Object>}
     */
    async get() {
        const response = await fetch('/api/settings');

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.error || 'Failed to get settings');
        }

        return response.json();
    }

    /**
     * Save settings.
     * @param {Object} settings - The settings to save
     * @returns {Promise<{success: boolean}>}
     */
    async save(settings) {
        const response = await fetch('/api/settings', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': typeof getCsrfToken === 'function' ? getCsrfToken() : '',
            },
            body: JSON.stringify(settings),
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.error || 'Failed to save settings');
        }

        return response.json();
    }
}

/**
 * Setup Wizard Manager
 *
 * Manages setup wizard state and progress.
 */
class SetupManager {
    /**
     * Check setup status.
     * @returns {Promise<{configured: boolean, setup_complete: boolean, current_step: number, plex_authenticated: boolean}>}
     */
    async getStatus() {
        const response = await fetch('/api/setup/status');
        return response.json();
    }

    /**
     * Get current setup state.
     * @returns {Promise<{step: number, data: Object}>}
     */
    async getState() {
        const response = await fetch('/api/setup/state');

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.error || 'Failed to get setup state');
        }

        return response.json();
    }

    /**
     * Save setup state.
     * @param {number} step - Current step number
     * @param {Object} data - Step data
     * @returns {Promise<{success: boolean}>}
     */
    async saveState(step, data) {
        const response = await fetch('/api/setup/state', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': typeof getCsrfToken === 'function' ? getCsrfToken() : '',
            },
            body: JSON.stringify({ step, data }),
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.error || 'Failed to save setup state');
        }

        return response.json();
    }

    /**
     * Complete setup.
     * @returns {Promise<{success: boolean, redirect: string}>}
     */
    async complete() {
        const response = await fetch('/api/setup/complete', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': typeof getCsrfToken === 'function' ? getCsrfToken() : '',
            },
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.error || 'Failed to complete setup');
        }

        return response.json();
    }
}

// Export for use in other scripts
window.PlexAuth = PlexAuth;
window.PlexServerManager = PlexServerManager;
window.SettingsManager = SettingsManager;
window.SetupManager = SetupManager;
