from fastapi.testclient import TestClient

from app.main import app


def test_unscoped_routers_are_unavailable_anonymously() -> None:
    client = TestClient(app)

    for path in (
        "/api/v1/agentic/strategy/hello",
        "/api/v1/graph/health",
        "/api/v1/validate/quick",
        "/api/v1/evaluation/test-cases",
    ):
        response = client.get(path)

        assert response.status_code == 404


def test_health_route_remains_available() -> None:
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_untrusted_origin_preflight_is_rejected() -> None:
    client = TestClient(app)

    response = client.options(
        "/health",
        headers={
            "Origin": "https://untrusted.example",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
