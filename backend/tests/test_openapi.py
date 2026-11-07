"""Swagger описывает ответы так, как их реально отдаёт API."""

from httpx import AsyncClient


async def test_422_schema_matches_actual_response(client: AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()
    schema = spec["components"]["schemas"]["HTTPValidationError"]
    assert schema["properties"]["detail"]["type"] == "string"
    assert schema["properties"]["errors"]["type"] == "array"

    op = spec["paths"]["/api/auth/register"]["post"]
    ref = op["responses"]["422"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("/HTTPValidationError")

    # Реальный ответ соответствует схеме.
    body = (await client.post("/api/auth/register", json={})).json()
    assert isinstance(body["detail"], str)
    assert isinstance(body["errors"], list)
    item_required = set(spec["components"]["schemas"]["ValidationError"]["required"])
    assert item_required <= set(body["errors"][0])


async def test_every_422_uses_project_schema(client: AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()
    for path, operations in spec["paths"].items():
        for method, op in operations.items():
            response = op.get("responses", {}).get("422")
            if response is None:
                continue
            ref = response["content"]["application/json"]["schema"]["$ref"]
            assert ref.endswith("/HTTPValidationError"), f"{method.upper()} {path}"
            assert response["description"] == "Неверный формат полей"
