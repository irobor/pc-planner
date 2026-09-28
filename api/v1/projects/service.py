import aiohttp
import asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.models.project import Project  # Твоя SQLAlchemy модель
from app.api.v1.projects.schemas import ProjectRead

class ProjectService:
    @staticmethod
    async def get_projects_with_time(db: AsyncSession) -> list[ProjectRead]:
        # 1. Работаем с БД напрямую (заменяем CRUD)
        query = select(Project)
        result = await db.execute(query)
        projects_db = result.scalars().all()

        # 2. Превращаем в Pydantic схемы
        results = [ProjectRead.model_validate(p) for p in projects_db]

        # 3. Бизнес-логика (внешний API)
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            tasks = [ProjectService._fetch_time(session, p) for p in results]
            await asyncio.gather(*tasks)
            
        return results

    @staticmethod
    async def _fetch_time(session: aiohttp.ClientSession, project_res: ProjectRead):
        url = f'https://timeapi.io/api/v1/timezone/zone?timeZone={project_res.timezone}'
        try:
            async with session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    lt = data.get('local_time', "")
                    project_res.current_time = lt.split('T')[1][:8] if 'T' in lt else lt
                else:
                    project_res.current_time = "API Error"
        except Exception:
            project_res.current_time = "Timeout"

project_service = ProjectService()