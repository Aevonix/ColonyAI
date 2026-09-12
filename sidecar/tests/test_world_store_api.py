"""World entities and relationships persist through the current HTTP interface."""

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from pacomind.api.routers import host
from pacomind.world_model.config import WorldModelConfig
from pacomind.world_model.store import WorldModelStore


async def test_world_http_entities_and_relationships_survive_reopen(tmp_path, monkeypatch):
    config = WorldModelConfig(sqlite_path=str(tmp_path / "world.db"))
    store = WorldModelStore(config)
    await store.connect()
    monkeypatch.setattr(host, "_world_store", store)
    app = FastAPI()
    app.include_router(host.router)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture") as client:
            person = await client.post("/v1/host/world/entities", json={
                "name": "Morgan", "entity_type": "person", "confidence": 0.8,
            })
            project = await client.post("/v1/host/world/entities", json={
                "name": "Weather station", "entity_type": "concept", "confidence": 0.8,
            })
            assert person.status_code == project.status_code == 200
            person_id, project_id = person.json()["id"], project.json()["id"]
            relationship = await client.post("/v1/host/world/relationships", json={
                "source_id": person_id, "target_id": project_id,
                "relationship_type": "WM_WORKS_ON", "confidence": 0.8,
            })
            assert relationship.status_code == 200, relationship.text
            relationship_id = relationship.json()["id"]
            await store.close()
            store = WorldModelStore(config)
            await store.connect()
            monkeypatch.setattr(host, "_world_store", store)
            restored = await client.get(f"/v1/host/world/entities/{person_id}")
            assert restored.status_code == 200
            assert restored.json()["name"] == "Morgan"
            health = await client.get("/v1/host/health")
            assert health.status_code == 200
            assert "world_model" in health.json()["capabilities"]
            rows = await client.get("/v1/host/world/relationships", params={"source_id": person_id})
            assert rows.status_code == 200
            assert any(row["id"] == relationship_id and row["target_id"] == project_id
                       for row in rows.json()["relationships"])
    finally:
        await store.close()
