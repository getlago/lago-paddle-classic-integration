from pydantic import BaseModel
from typing import Optional


class LagoCustomer(BaseModel):
    lago_id: str
    external_id: str
    name: Optional[str] = None
    email: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zipcode: Optional[str] = None
    country: Optional[str] = None
    legal_name: Optional[str] = None
    tax_identification_number: Optional[str] = None

