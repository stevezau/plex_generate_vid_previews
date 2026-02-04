"""
Tests for Plex OAuth API routes.

Tests the OAuth authentication flow and settings API endpoints.
"""

import json
import pytest
from unittest.mock import patch, MagicMock
import requests as real_requests


@pytest.fixture
def mock_auth_config(tmp_path, monkeypatch):
    """Mock auth module to use temp directory."""
    auth_file = str(tmp_path / 'auth.json')
    monkeypatch.setattr('plex_generate_previews.web.auth.AUTH_FILE', auth_file)
    monkeypatch.setattr('plex_generate_previews.web.auth.get_config_dir', lambda: str(tmp_path))
    
    # Reset the global settings manager singleton
    from plex_generate_previews.web.settings_manager import reset_settings_manager
    reset_settings_manager()
    
    return str(tmp_path)


@pytest.fixture
def flask_app(tmp_path, mock_auth_config):
    """Create Flask app for testing."""
    from plex_generate_previews.web.app import create_app
    app = create_app(config_dir=str(tmp_path))
    app.config['TESTING'] = True
    return app


@pytest.fixture
def client(flask_app):
    """Create test client."""
    return flask_app.test_client()


@pytest.fixture
def auth_headers(flask_app):
    """Get auth headers for API calls."""
    from plex_generate_previews.web.auth import get_auth_token
    token = get_auth_token()
    return {'X-Auth-Token': token}


class TestSettingsAPIRoutes:
    """Tests for settings API endpoints."""
    
    def test_get_settings(self, client, auth_headers):
        """Test getting current settings."""
        response = client.get('/api/settings', headers=auth_headers)
        
        assert response.status_code == 200
        data = json.loads(response.data)
        # Check that settings fields are present
        assert 'plex_url' in data
        assert 'gpu_threads' in data
        assert 'thumbnail_interval' in data
    
    def test_update_settings(self, client, auth_headers):
        """Test updating settings."""
        response = client.post(
            '/api/settings',
            headers={**auth_headers, 'Content-Type': 'application/json'},
            data=json.dumps({
                'gpu_threads': 4,
                'thumbnail_interval': 5
            })
        )
        
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True
        
        # Verify settings were saved
        response = client.get('/api/settings', headers=auth_headers)
        data = json.loads(response.data)
        assert data['gpu_threads'] == 4
        assert data['thumbnail_interval'] == 5
    
    def test_update_plex_url(self, client, auth_headers):
        """Test updating plex_url setting."""
        response = client.post(
            '/api/settings',
            headers={**auth_headers, 'Content-Type': 'application/json'},
            data=json.dumps({
                'plex_url': 'http://192.168.1.100:32400'
            })
        )
        
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True


class TestSetupRoutes:
    """Tests for setup wizard API endpoints."""
    
    def test_get_setup_status(self, client):
        """Test getting setup status (no auth required)."""
        response = client.get('/api/setup/status')
        
        assert response.status_code == 200
        data = json.loads(response.data)
        # Check actual API response fields
        assert 'configured' in data
        assert 'setup_complete' in data
        assert 'current_step' in data
        assert 'plex_authenticated' in data
    
    def test_save_setup_state(self, client, auth_headers):
        """Test saving setup wizard state."""
        response = client.post(
            '/api/setup/state',
            headers={**auth_headers, 'Content-Type': 'application/json'},
            data=json.dumps({
                'step': 2,
                'data': {'server_name': 'Test Server'}
            })
        )
        
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True
        
        # Verify state was saved - check via get_setup_state
        response = client.get('/api/setup/state', headers=auth_headers)
        data = json.loads(response.data)
        assert data['step'] == 2
    
    def test_complete_setup(self, client, auth_headers):
        """Test completing the setup wizard."""
        # First save some settings
        client.post(
            '/api/settings',
            headers={**auth_headers, 'Content-Type': 'application/json'},
            data=json.dumps({
                'plex_url': 'http://localhost:32400',
                'plex_token': 'test-token'
            })
        )
        
        response = client.post('/api/setup/complete', headers=auth_headers)
        
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True
        
        # Verify setup is marked complete
        response = client.get('/api/setup/status')
        data = json.loads(response.data)
        assert data['setup_complete'] is True


class TestPlexServerRoutes:
    """Tests for Plex server discovery routes."""
    
    def test_get_servers_without_token(self, client, auth_headers):
        """Test getting servers without Plex token returns error."""
        response = client.get('/api/plex/servers', headers=auth_headers)
        
        # Should return error since no Plex token is configured
        assert response.status_code in [400, 401, 500]
    
    def test_get_libraries_without_server(self, client, auth_headers):
        """Test getting libraries without server configured."""
        response = client.get('/api/plex/libraries', headers=auth_headers)
        
        # Should return error since no server is configured
        assert response.status_code in [400, 500]


class TestAuthRequired:
    """Tests for authentication requirement on API endpoints."""
    
    def test_settings_requires_auth(self, client):
        """Test that settings endpoint requires authentication."""
        response = client.get('/api/settings')
        assert response.status_code == 401
    
    def test_save_settings_requires_auth(self, client):
        """Test that save settings endpoint requires authentication."""
        response = client.post(
            '/api/settings',
            headers={'Content-Type': 'application/json'},
            data=json.dumps({'gpu_threads': 4})
        )
        assert response.status_code == 401
    
    def test_invalid_token_rejected(self, client):
        """Test that invalid token is rejected."""
        response = client.get(
            '/api/settings',
            headers={'X-Auth-Token': 'invalid-token-12345'}
        )
        assert response.status_code == 401


class TestJobLogsAndWorkers:
    """Tests for job logs and worker status endpoints."""
    
    def test_get_job_logs_not_found(self, client, auth_headers):
        """Test getting logs for non-existent job returns 404."""
        response = client.get('/api/jobs/nonexistent-job-id/logs', headers=auth_headers)
        assert response.status_code == 404
    
    def test_get_worker_statuses(self, client, auth_headers):
        """Test getting worker statuses returns array."""
        response = client.get('/api/jobs/workers', headers=auth_headers)
        assert response.status_code == 200
        data = json.loads(response.data)
        assert 'workers' in data
        assert isinstance(data['workers'], list)
    
    def test_job_logs_requires_auth(self, client):
        """Test that job logs endpoint requires authentication."""
        response = client.get('/api/jobs/some-job-id/logs')
        assert response.status_code == 401
    
    def test_workers_requires_auth(self, client):
        """Test that workers endpoint requires authentication."""
        response = client.get('/api/jobs/workers')
        assert response.status_code == 401
