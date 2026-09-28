from pydantic import BaseModel, ConfigDict

# Базовые поля
class ProjectBase(BaseModel):
    name: str
    timezone: str

# Схема для создания (POST запрос)
class ProjectCreate(ProjectBase):
    pass

# Схема для ответа (GET запрос)
class ProjectRead(ProjectBase):
    id: int
    current_time: str = "" # Наше динамическое поле!

    # Новый стандарт Pydantic V2 для работы с ORM (SQLAlchemy)
    model_config = ConfigDict(from_attributes=True)