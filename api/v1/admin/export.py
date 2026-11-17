# app/api/v1/admin/export.py
"""Выгрузка сводного отчёта в Excel (.xlsx).

Чистые функции: на входе готовый SummaryReport, на выходе байты файла и
его имя. Базы здесь нет — отчёт строит сервис, а файл показывает ровно
то же, что страница: те же строки, итоги и правило нулей (в строках
пусто, в итогах 0). Проверяется в консоли на готовом отчёте.

Печать отчёта делается из Excel, поэтому параметры печати заложены в
файл: альбомная ориентация, по ширине в одну страницу, шапка таблицы
повторяется на каждом листе.
"""

from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.api.v1.admin.service import (
    ReportMode,
    SummaryBureau,
    SummaryReport,
    SummaryRow,
)

# Раскладка листа: заголовок в первой строке, шапка таблицы — в двух
# строках (отделы, затем бюро), данные — сразу под ней.
TITLE_ROW = 1
HEADER_TOP = 3
HEADER_BOTTOM = HEADER_TOP + 1
FIRST_DATA_ROW = HEADER_BOTTOM + 1

# Номер проекта, заказчик, наименование — до колонок бюро.
TEXT_COLUMNS = 3

# Ширина колонок в символах. Наименование в файле не обрезается: колонка
# широкая, полный текст в ячейке.
WIDTH_CODE = 10
WIDTH_CUSTOMER = 30
WIDTH_TITLE = 50
WIDTH_BUREAU = 7
WIDTH_TOTAL = 10
WIDTH_HOURS = 12

MODE_TITLES: dict[ReportMode, str] = {
    ReportMode.FACT: "Факт",
    ReportMode.PLAN: "Плановые затраты",
}
MODE_FILE_TOKENS: dict[ReportMode, str] = {
    ReportMode.FACT: "факт",
    ReportMode.PLAN: "план",
}

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_THIN = Side(style="thin", color="A6A6A6")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_BOLD = Font(bold=True)
# Фон шапки и строки «Всего» — светло-серый, как table-light на экране.
_LIGHT_FILL = PatternFill(fill_type="solid", start_color="F2F2F2")
_HEADER_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=True)


def summary_report_filename(report: SummaryReport) -> str:
    """Имя файла: Отчёт_факт_2026-11-02_2026-11-10.xlsx."""
    token = MODE_FILE_TOKENS[report.mode]
    return (
        f"Отчёт_{token}_{report.date_from.isoformat()}"
        f"_{report.date_to.isoformat()}.xlsx"
    )


def summary_report_xlsx(report: SummaryReport) -> bytes:
    """Собирает .xlsx из готового отчёта и возвращает байты файла."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Отчёт"

    bureaus = report.bureaus
    total_col = TEXT_COLUMNS + len(bureaus) + 1
    hours_col = total_col + 1

    # Заголовок: распечатанный или пересланный файл без него не скажет,
    # какой это отчёт и за какой период.
    period = f"{report.date_from:%d.%m.%Y} – {report.date_to:%d.%m.%Y}"
    title = ws.cell(TITLE_ROW, 1)
    title.value = f"Отчёт затраченных ресурсов — {MODE_TITLES[report.mode]} за {period}"
    title.font = Font(bold=True, size=12)

    _write_header(ws, report, total_col, hours_col)

    row = FIRST_DATA_ROW
    for project in report.projects:
        _write_row(ws, row, project, bureaus)
        row += 1
    if not report.projects:
        ws.cell(row, 1).value = "Нет данных за период"
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=hours_col)
        row += 1
    _write_row(ws, row, report.projects_total, bureaus, is_total=True)
    row += 1
    for absence in report.absences:
        _write_row(ws, row, absence, bureaus)
        row += 1
    _write_row(ws, row, report.absences_total, bureaus, is_total=True)
    row += 1
    _write_row(ws, row, report.grand_total, bureaus, is_total=True)
    for col in range(1, hours_col + 1):
        ws.cell(row, col).fill = _LIGHT_FILL

    # Рамки — на всю таблицу, включая ячейки, скрытые объединением:
    # иначе у объединённой ячейки пропадает часть контура.
    for table_row in ws.iter_rows(
        min_row=HEADER_TOP, max_row=row, min_col=1, max_col=hours_col
    ):
        for cell in table_row:
            cell.border = _BORDER

    _set_widths(ws, len(bureaus))

    # При прокрутке видны шапка и три первые колонки. Адрес строкой, а не
    # ячейкой: в пустом отчёте эта ячейка — часть объединённой строки
    # «Нет данных за период», и openpyxl такую не принимает.
    ws.freeze_panes = f"{get_column_letter(TEXT_COLUMNS + 1)}{FIRST_DATA_ROW}"

    # Печать — из Excel: альбом, по ширине в одну страницу, по высоте
    # сколько понадобится, шапка на каждом листе.
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = f"{HEADER_TOP}:{HEADER_BOTTOM}"

    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _write_header(
    ws: Worksheet, report: SummaryReport, total_col: int, hours_col: int
) -> None:
    """Шапка в две строки: отделы над своими бюро, затем коды бюро."""
    # Колонки на две строки шапки: текстовые слева, итоговые справа.
    spanning = [
        (1, "Номер проекта"),
        (2, "Заказчик"),
        (3, "Наименование работ"),
        (total_col, "Итого (дни)"),
        (hours_col, "Чел.-часы (×8)"),
    ]
    for col, label in spanning:
        _header_cell(ws, HEADER_TOP, col, label)
        ws.merge_cells(
            start_row=HEADER_TOP,
            start_column=col,
            end_row=HEADER_BOTTOM,
            end_column=col,
        )

    # Отделы — в порядке колонок (report.departments), объединены над
    # своими бюро.
    col = TEXT_COLUMNS + 1
    for code, _name, span in report.departments:
        _header_cell(ws, HEADER_TOP, col, code)
        if span > 1:
            ws.merge_cells(
                start_row=HEADER_TOP,
                start_column=col,
                end_row=HEADER_TOP,
                end_column=col + span - 1,
            )
        col += span

    for offset, bureau in enumerate(report.bureaus):
        _header_cell(ws, HEADER_BOTTOM, TEXT_COLUMNS + 1 + offset, bureau.code)

    # Фон — на обе строки шапки целиком, включая скрытые объединением.
    for header_row in ws.iter_rows(
        min_row=HEADER_TOP, max_row=HEADER_BOTTOM, min_col=1, max_col=hours_col
    ):
        for cell in header_row:
            cell.fill = _LIGHT_FILL


def _header_cell(ws: Worksheet, row: int, col: int, label: str) -> None:
    cell = ws.cell(row, col)
    cell.value = label
    cell.font = _BOLD
    cell.alignment = _HEADER_ALIGN


def _write_row(
    ws: Worksheet,
    row: int,
    data: SummaryRow,
    bureaus: list[SummaryBureau],
    *,
    is_total: bool = False,
) -> None:
    """Одна строка таблицы — то же правило, что у макроса report_row.

    Итоговая строка пишет подпись на три первые колонки (объединены) и
    показывает нули; обычная — номер, заказчика и наименование, нули
    оставляет пустыми.
    """
    if is_total:
        ws.cell(row, 1).value = data.title
        ws.merge_cells(
            start_row=row, start_column=1, end_row=row, end_column=TEXT_COLUMNS
        )
    else:
        ws.cell(row, 1).value = data.code
        ws.cell(row, 2).value = data.customer
        ws.cell(row, 3).value = data.title

    for offset, bureau in enumerate(bureaus):
        days = data.days.get(bureau.id, 0)
        if days or is_total:
            ws.cell(row, TEXT_COLUMNS + 1 + offset).value = days

    total_col = TEXT_COLUMNS + len(bureaus) + 1
    if data.total or is_total:
        ws.cell(row, total_col).value = data.total
        ws.cell(row, total_col + 1).value = data.hours

    if is_total:
        for col in range(1, total_col + 2):
            ws.cell(row, col).font = _BOLD


def _set_widths(ws: Worksheet, bureau_count: int) -> None:
    widths = [WIDTH_CODE, WIDTH_CUSTOMER, WIDTH_TITLE]
    widths += [WIDTH_BUREAU] * bureau_count
    widths += [WIDTH_TOTAL, WIDTH_HOURS]
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width