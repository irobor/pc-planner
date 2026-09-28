# app/api/v1/calendar/service.py
from collections.abc import Sequence

from sqlalchemy import RowMapping, text
from sqlalchemy.ext.asyncio import AsyncSession


class CalendarService:
    """Доступ к производственному календарю."""

    @staticmethod
    async def get_january_2025(db: AsyncSession) -> Sequence[RowMapping]:
        """Возвращает дни января 2025 года с типом дня и днём недели.

        Источник — нормализованный производственный календарь
        (production_calendar + справочник day_types). Ранее метод читал
        отладочное представление v_test_january_2025_w, которого в базе
        больше нет: одноразовые объекты под конкретный эксперимент не
        переживают перезаливку схемы.

        Возвращает Sequence[RowMapping] — набор словареподобных строк,
        который Pydantic-модель TestCalendarResponse разбирает по именам
        колонок.
        """
        # ISODOW, а не DOW: понедельник = 1, воскресенье = 7. DOW нумерует
        # с воскресенья-нуля, и недельная сетка планировщика съехала бы
        # на день.
        #
        # Названия дней задаются CASE, а не to_char(..., 'TMDay'): та
        # локалезависима и на portable-сборке PostgreSQL под Windows
        # вернула бы язык из lc_time сервера — значение непредсказуемое
        # и меняется при переустановке.
        #
        # Границы полуинтервалом (>= начало, < начало следующего месяца):
        # при переходе колонки на timestamp BETWEEN потерял бы последний
        # день.
        query = text(
            """
            SELECT
                pc.work_date AS date,
                dt.code      AS day_type,
                EXTRACT(ISODOW FROM pc.work_date)::int AS day_of_week,
                CASE EXTRACT(ISODOW FROM pc.work_date)
                    WHEN 1 THEN 'Понедельник'
                    WHEN 2 THEN 'Вторник'
                    WHEN 3 THEN 'Среда'
                    WHEN 4 THEN 'Четверг'
                    WHEN 5 THEN 'Пятница'
                    WHEN 6 THEN 'Суббота'
                    WHEN 7 THEN 'Воскресенье'
                END AS day_name
            FROM public.production_calendar pc
            JOIN public.day_types dt ON dt.id = pc.day_type_id
            WHERE pc.work_date >= DATE '2025-01-01'
              AND pc.work_date <  DATE '2025-02-01'
            ORDER BY pc.work_date
            """
        )
        result = await db.execute(query)

        # mappings() отдаёт строки как словари: ключи совпадают с
        # алиасами SELECT и с полями TestCalendarResponse.
        return result.mappings().all()