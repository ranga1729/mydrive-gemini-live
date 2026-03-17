"""
TOOLS.py — Tool definitions and executor for MyDrive backend.

This file is a DROP-IN REPLACEMENT compatible with both the Gemini
and OpenAI backends.  The Gemini backend used Google's function
declaration format; this file exposes the SAME list in OpenAI's
Chat-Completions / Realtime tool format (which is also a superset
of the JSON Schema format Gemini used, so the same definitions work).

Replace the stub implementations below with your real business logic
(database lookups, external API calls, etc.)
"""

from __future__ import annotations
from typing import Any

# ──────────────────────────────────────────────────────────────
# Tool definitions — OpenAI function format
# (name, description, parameters as JSON Schema)
# ──────────────────────────────────────────────────────────────

TOOLS: list[dict] = [
    {
        "name": "request_roadside_assistance",
        "description": (
            "Request emergency roadside assistance for a broken-down vehicle. "
            "Use when the driver reports a breakdown, flat tyre, dead battery, "
            "or any situation requiring on-site mechanical help."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "Current location of the vehicle (address or GPS coordinates).",
                },
                "issue_description": {
                    "type": "string",
                    "description": "Brief description of the problem (e.g. 'flat tyre', 'won't start').",
                },
                "vehicle_registration": {
                    "type": "string",
                    "description": "Vehicle registration/license plate number.",
                },
            },
            "required": ["location", "issue_description"],
            "additionalProperties": False,
        },
    },
    {
        "name": "request_tow_truck",
        "description": (
            "Request a tow truck to transport the vehicle to a garage or safe location. "
            "Use when the vehicle cannot be repaired on-site."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pickup_location": {
                    "type": "string",
                    "description": "Location where the vehicle needs to be picked up.",
                },
                "destination": {
                    "type": "string",
                    "description": "Garage or destination where the vehicle should be towed.",
                },
                "vehicle_type": {
                    "type": "string",
                    "description": "Type of vehicle (e.g. 'sedan', 'SUV', 'motorcycle').",
                },
            },
            "required": ["pickup_location"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_spare_parts",
        "description": (
            "Search for available spare parts for a vehicle. "
            "Use when the driver needs to find or order replacement parts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "part_name": {
                    "type": "string",
                    "description": "Name or description of the spare part needed.",
                },
                "vehicle_make": {
                    "type": "string",
                    "description": "Vehicle manufacturer (e.g. 'Toyota', 'Honda').",
                },
                "vehicle_model": {
                    "type": "string",
                    "description": "Vehicle model (e.g. 'Corolla', 'Civic').",
                },
                "vehicle_year": {
                    "type": "integer",
                    "description": "Manufacturing year of the vehicle.",
                },
            },
            "required": ["part_name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "book_garage_service",
        "description": (
            "Book a service appointment at a garage or service center. "
            "Use when the driver wants to schedule maintenance or repair work."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "service_type": {
                    "type": "string",
                    "description": "Type of service required (e.g. 'oil change', 'brake inspection').",
                },
                "preferred_date": {
                    "type": "string",
                    "description": "Preferred date for the appointment (YYYY-MM-DD or natural language).",
                },
                "preferred_location": {
                    "type": "string",
                    "description": "Preferred garage location or area.",
                },
                "vehicle_registration": {
                    "type": "string",
                    "description": "Vehicle registration number.",
                },
            },
            "required": ["service_type"],
            "additionalProperties": False,
        },
    },
]


# ──────────────────────────────────────────────────────────────
# Tool executor
# ──────────────────────────────────────────────────────────────

def execute_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """
    Dispatch a tool call by name and return the result as a dict.

    Replace the stub implementations with real logic:
      - Database queries
      - External REST/gRPC API calls
      - Third-party SDK calls
    All implementations must be synchronous here (called from async context
    via standard call — wrap with asyncio.to_thread if needed for blocking I/O).
    """
    dispatch: dict[str, Any] = {
        "request_roadside_assistance": _request_roadside_assistance,
        "request_tow_truck":           _request_tow_truck,
        "search_spare_parts":          _search_spare_parts,
        "book_garage_service":         _book_garage_service,
    }

    handler = dispatch.get(name)
    if handler is None:
        return {"error": f"Unknown tool: {name!r}"}

    try:
        return handler(**args)
    except Exception as exc:
        return {"error": str(exc)}


# ──────────────────────────────────────────────────────────────
# Stub implementations — replace with real logic
# ──────────────────────────────────────────────────────────────

def _request_roadside_assistance(
    location: str,
    issue_description: str,
    vehicle_registration: str = "N/A",
) -> dict:
    # TODO: Call your roadside assistance API
    return {
        "status": "dispatched",
        "eta_minutes": 25,
        "reference": "RSA-20240001",
        "message": (
            f"Roadside assistance dispatched to {location!r}. "
            f"ETA approx 25 minutes. Reference: RSA-20240001."
        ),
    }


def _request_tow_truck(
    pickup_location: str,
    destination: str = "nearest garage",
    vehicle_type: str = "unknown",
) -> dict:
    # TODO: Call your tow-truck dispatch API
    return {
        "status": "confirmed",
        "eta_minutes": 40,
        "reference": "TOW-20240001",
        "message": (
            f"Tow truck confirmed from {pickup_location!r} to {destination!r}. "
            f"ETA approx 40 minutes. Reference: TOW-20240001."
        ),
    }


def _search_spare_parts(
    part_name: str,
    vehicle_make: str = "",
    vehicle_model: str = "",
    vehicle_year: int = 0,
) -> dict:
    # TODO: Query your parts inventory / supplier API
    return {
        "status": "found",
        "results": [
            {
                "supplier":   "AutoParts Central",
                "part":       part_name,
                "price":      "LKR 4,500",
                "in_stock":   True,
                "delivery":   "Same day",
            }
        ],
        "message": f"Found 1 result for {part_name!r}.",
    }


def _book_garage_service(
    service_type: str,
    preferred_date: str = "as soon as possible",
    preferred_location: str = "nearest",
    vehicle_registration: str = "N/A",
) -> dict:
    # TODO: Integrate with your garage booking system
    return {
        "status": "booked",
        "booking_id": "GRG-20240001",
        "confirmed_date": preferred_date,
        "garage": "AutoFix Service Centre, Colombo 03",
        "message": (
            f"Service appointment booked for {service_type!r} on "
            f"{preferred_date}. Booking ID: GRG-20240001."
        ),
    }