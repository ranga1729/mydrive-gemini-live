from asyncio import log
from typing import Any

TOOLS: list[dict[str, Any]] = [
    {
        "name": "request_roadside_assistance",
        "description": (
            "Dispatches a roadside assistance unit. Use for flat tyres, dead batteries, "
            "fuel delivery, locked-out vehicles, and any minor roadside issue that does "
            "not require towing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "issue_description": {
                    "type": "string",
                    "description": "Brief description of the roadside issue.",
                },
                "vehicle_info": {
                    "type": "string",
                    "description": "Vehicle make/model if mentioned, otherwise 'unknown'.",
                },
            },
            "required": ["issue_description"],
        },
    },
    {
        "name": "request_tow_truck",
        "description": (
            "Dispatches a tow truck. Use for accidents, non-starting cars, major mechanical "
            "failures, overheating, or any situation where the vehicle cannot be driven safely."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "issue_description": {
                    "type": "string",
                    "description": "Brief description of why towing is needed.",
                },
                "vehicle_info": {
                    "type": "string",
                    "description": "Vehicle make/model if mentioned, otherwise 'unknown'.",
                },
            },
            "required": ["issue_description"],
        },
    },
    {
        "name": "search_spare_parts",
        "description": (
            "Searches the MyDrive spare parts marketplace. Use when the user wants "
            "to find, order, or enquire about car parts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "part_name": {
                    "type": "string",
                    "description": "Name or description of the spare part.",
                },
                "vehicle_info": {
                    "type": "string",
                    "description": "Vehicle make/model/year if mentioned, otherwise 'unknown'.",
                },
            },
            "required": ["part_name"],
        },
    },
    {
        "name": "book_garage_service",
        "description": (
            "Books a garage service appointment. Use for routine maintenance, "
            "unusual sounds/warning lights, or any situation needing a garage inspection."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "service_type": {
                    "type": "string",
                    "description": "Type of garage service or inspection needed.",
                },
                "vehicle_info": {
                    "type": "string",
                    "description": "Vehicle make/model if mentioned, otherwise 'unknown'.",
                },
            },
            "required": ["service_type"],
        },
    },
]

# ──────────────────────────────────────────────────────────────
# Tool implementations
# ──────────────────────────────────────────────────────────────

def _tool_roadside(issue_description: str, vehicle_info: str = "unknown") -> dict:
  log.info("TOOL roadside | issue=%r vehicle=%r", issue_description, vehicle_info)
  return {"status": "dispatched", "service": "roadside_assistance", "eta_minutes": 20}

def _tool_tow_truck(issue_description: str, vehicle_info: str = "unknown") -> dict:
  log.info("TOOL tow_truck | issue=%r vehicle=%r", issue_description, vehicle_info)
  return {"status": "dispatched", "service": "tow_truck", "eta_minutes": 35}

def _tool_spare_parts(part_name: str, vehicle_info: str = "unknown") -> dict:
  log.info("TOOL spare_parts | part=%r vehicle=%r", part_name, vehicle_info)
  return {"status": "search_initiated", "part": part_name, "results_count": 12}

def _tool_garage(service_type: str, vehicle_info: str = "unknown") -> dict:
  log.info("TOOL garage | service=%r vehicle=%r", service_type, vehicle_info)
  return {
    "status": "booking_initiated",
    "service_type": service_type,
    "next_available": "tomorrow 10:00 AM",
  }

_TOOL_REGISTRY: dict[str, Any] = {
  "request_roadside_assistance": _tool_roadside,
  "request_tow_truck":           _tool_tow_truck,
  "search_spare_parts":          _tool_spare_parts,
  "book_garage_service":         _tool_garage,
}

def execute_tool(name: str, args: dict) -> dict:
  fn = _TOOL_REGISTRY.get(name)
  if fn is None:
    log.warning("Unknown tool requested: %s", name)
    return {"error": f"Unknown tool: {name}"}
  try:
    return fn(**args)
  except TypeError as exc:
    log.error("Tool %s called with bad args %s: %s", name, args, exc)
    return {"error": f"Invalid arguments for {name}: {exc}"}
  except Exception as exc:
    log.exception("Tool %s raised an unexpected error", name)
    return {"error": str(exc)}
