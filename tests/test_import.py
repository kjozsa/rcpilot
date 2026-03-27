"""Test the GitHub import functionality."""

import pytest
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient
from pilot.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_import_project_success(client, tmp_path, monkeypatch):
    """Test successful project import."""
    # Mock the config to use a temporary directory
    from pilot import main
    original_projects_dir = main._config.projects_dir
    main._config.projects_dir = tmp_path
    
    try:
        # Mock subprocess.run to simulate successful git clone
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout='', stderr='')
            
            response = client.post(
                '/api/projects/import',
                json={'repo_url': 'https://github.com/user/test-repo.git'}
            )
            
            assert response.status_code == 200
            data = response.json()
            assert data['success'] is True
            assert data['project_name'] == 'test-repo'
            assert 'Successfully imported' in data['message']
            
            # Verify git clone was called with correct arguments
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert args[0] == 'git'
            assert args[1] == 'clone'
            assert args[2] == 'https://github.com/user/test-repo.git'
    finally:
        main._config.projects_dir = original_projects_dir


def test_import_project_invalid_url(client):
    """Test import with invalid URL format."""
    response = client.post(
        '/api/projects/import',
        json={'repo_url': 'not-a-valid-url'}
    )
    
    assert response.status_code == 422
    assert 'Invalid repository URL format' in response.json()['detail']


def test_import_project_empty_url(client):
    """Test import with empty URL."""
    response = client.post(
        '/api/projects/import',
        json={'repo_url': ''}
    )
    
    assert response.status_code == 422
    assert 'Repository URL is required' in response.json()['detail']


def test_import_project_already_exists(client, tmp_path, monkeypatch):
    """Test import when project already exists."""
    from pilot import main
    original_projects_dir = main._config.projects_dir
    main._config.projects_dir = tmp_path
    
    try:
        # Create a directory that simulates an existing project
        existing_project = tmp_path / 'existing-repo'
        existing_project.mkdir()
        
        response = client.post(
            '/api/projects/import',
            json={'repo_url': 'https://github.com/user/existing-repo.git'}
        )
        
        assert response.status_code == 409
        assert "already exists" in response.json()['detail']
    finally:
        main._config.projects_dir = original_projects_dir


def test_import_project_git_clone_fails(client, tmp_path, monkeypatch):
    """Test import when git clone fails."""
    from pilot import main
    original_projects_dir = main._config.projects_dir
    main._config.projects_dir = tmp_path
    
    try:
        with patch('subprocess.run') as mock_run:
            mock_run.return_value = Mock(
                returncode=128,
                stdout='',
                stderr='fatal: repository not found'
            )
            
            response = client.post(
                '/api/projects/import',
                json={'repo_url': 'https://github.com/user/nonexistent.git'}
            )
            
            assert response.status_code == 500
            assert 'Git clone failed' in response.json()['detail']
    finally:
        main._config.projects_dir = original_projects_dir
