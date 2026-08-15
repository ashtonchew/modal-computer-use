from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from modal_computer_use.daemon.schemas import DaemonErrorResponse

_HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
_PUBLIC_PROBES = {"/healthz", "/readyz"}
_RAW_SCREENSHOT_PATHS = {
    "/v1/actions/run/raw-screenshot",
    "/v1/actions/run/observe-change/raw-screenshot",
}


def openapi_schema(app: FastAPI) -> dict[str, Any]:
    """Describe the middleware and exception envelopes that runtime routes use."""

    if app.openapi_schema is not None:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    components = schema.setdefault("components", {})
    components.setdefault("securitySchemes", {})["bearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "description": (
            "Daemon bearer or tunnel token. Modal Connect requests use the authenticated proxy."
        ),
    }
    schemas = components.setdefault("schemas", {})
    schemas["DaemonErrorResponse"] = DaemonErrorResponse.model_json_schema(
        ref_template="#/components/schemas/{model}"
    )
    for path, path_item in schema.get("paths", {}).items():
        if path in _PUBLIC_PROBES:
            continue
        for method, operation in path_item.items():
            if method not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            operation["security"] = [{"bearerAuth": []}]
            responses = operation.setdefault("responses", {})
            validation = responses.get("422")
            if isinstance(validation, dict):
                validation["content"] = {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/DaemonErrorResponse"}
                    }
                }
            if path in _RAW_SCREENSHOT_PATHS:
                _hide_raw_screenshot_idempotency_field(operation, schemas)
    schemas.pop("HTTPValidationError", None)
    schemas.pop("ValidationError", None)
    app.openapi_schema = schema
    return schema


def _hide_raw_screenshot_idempotency_field(
    operation: dict[str, Any],
    schemas: dict[str, Any],
) -> None:
    request_body = operation.get("requestBody")
    if not isinstance(request_body, dict):
        return
    content = request_body.get("content")
    if not isinstance(content, dict):
        return
    application_json = content.get("application/json")
    if not isinstance(application_json, dict):
        return
    reference = application_json.get("schema")
    if not isinstance(reference, dict):
        return
    component_ref = reference.get("$ref")
    if not isinstance(component_ref, str) or not component_ref.startswith(
        "#/components/schemas/"
    ):
        return
    component_name = component_ref.rsplit("/", 1)[-1]
    original = schemas.get(component_name)
    if not isinstance(original, dict):
        return
    raw_name = f"{component_name}WithoutIdempotency"
    if raw_name not in schemas:
        raw_schema = {
            key: value.copy() if isinstance(value, dict) else value
            for key, value in original.items()
        }
        properties = raw_schema.get("properties")
        if isinstance(properties, dict):
            properties = properties.copy()
            properties.pop("idempotency_key", None)
            raw_schema["properties"] = properties
        raw_schema["title"] = raw_name
        schemas[raw_name] = raw_schema
    application_json["schema"] = {"$ref": f"#/components/schemas/{raw_name}"}
