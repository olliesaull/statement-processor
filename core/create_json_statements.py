"""Module for turning pdf text into structured JSON"""

import json

from ollama import chat
from pydantic import BaseModel, ValidationError


class LineItem(BaseModel):
    description: str
    quantity: int
    unit_price: float

class Invoice(BaseModel):
    invoice_number: str
    vendor: str
    date: str          # ISO 8601 string
    currency: str = "GBP"
    total: float
    items: list[LineItem]

def create_structured_json(messages):
    resp = chat(
        model="gemma3:27b",
        messages=messages,
        format=Invoice.model_json_schema(),
        options={"temperature": 0}  # more deterministic
    )

    raw = resp.message.content

    try:
        # Step 1: parse model output into dict
        parsed = json.loads(raw)

        # # Step 2: validate against Pydantic model
        # invoice = Invoice(**parsed)
        return parsed

    except json.JSONDecodeError:
        raise ValueError(f"Model did not return valid JSON: {raw}")
    except ValidationError as e:
        raise ValueError(f"Model output failed validation: {e}")
