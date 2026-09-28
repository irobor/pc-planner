# app\api\v1\calendar\schemas.py
from pydantic import BaseModel
from datetime import date

class TestCalendarResponse(BaseModel):
    date: date
    day_type: str
    day_of_week: int  # Новое поле из базы
    day_name: str     # Новое поле из базы

    class Config:
        from_attributes = True