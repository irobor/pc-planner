from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # Pydantic ищет переменные с такими же именами в .env (регистр не важен)
    DATABASE_URL: str
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # Указываем путь к .env файлу и добавляем extra="ignore"
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore"  # <- ДОБАВЬ ЭТУ СТРОКУ
    )

# Создаем экземпляр настроек, который будем импортировать
settings = Settings()