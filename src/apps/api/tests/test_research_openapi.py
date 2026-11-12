from fastapi.testclient import TestClient
import json
from pathlib import Path

from app.main import app


def test_research_schema_is_separate_and_references_resolve():
    client = TestClient(app)
    legacy = client.get("/openapi.json").json()
    response = client.get("/v2/research/openapi.json")
    assert response.status_code == 200
    document = response.json()
    snapshot = Path(__file__).resolve().parents[3] / "packages/contracts/openapi/v2/openapi.json"
    assert document == json.loads(snapshot.read_text())
    assert "/v2/research/topics" in document["paths"]
    assert "/v2/research/topics" not in legacy["paths"]
    assert "ResearchTopic" in document["components"]["schemas"]

    def check(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "$ref" and isinstance(item, str) and item.startswith("#/components/schemas/"):
                    assert item.split("/")[-1] in document["components"]["schemas"]
                else:
                    check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
    check(document)
