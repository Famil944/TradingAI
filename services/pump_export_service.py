import json
from io import BytesIO
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile


CHECKPOINTS = ("5", "15", "30", "60", "180", "360", "720", "1440")


def _column(number):
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _cell(reference, value, header=False):
    if value is None:
        return f'<c r="{reference}"/>'
    style = ' s="1"' if header else ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{reference}"{style}><v>{value}</v></c>'
    return (
        f'<c r="{reference}" t="inlineStr"{style}><is><t>'
        f'{escape(str(value))}</t></is></c>'
    )


def _worksheet(rows):
    if not rows:
        rows = [("Нет данных",)]
    xml_rows = []
    for row_number, row in enumerate(rows, 1):
        cells = "".join(
            _cell(f"{_column(column)}{row_number}", value, row_number == 1)
            for column, value in enumerate(row, 1)
        )
        xml_rows.append(f'<row r="{row_number}">{cells}</row>')
    last_cell = f"{_column(max(len(row) for row in rows))}{len(rows)}"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:{last_cell}"/><sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" state="frozen"/></sheetView></sheetViews>'
        f'<sheetData>{"".join(xml_rows)}</sheetData><autoFilter ref="A1:{last_cell}"/>'
        '</worksheet>'
    )


def _percent(price, start):
    return round((float(price) / float(start) - 1) * 100, 4) if start else None


def build_pump_xlsx(predictions: list[dict]) -> bytes:
    completed = [row for row in predictions if row.get("status") == "completed"]
    successful = [row for row in completed if row.get("outcome") == "pump"]
    summary = [
        ("Показатель", "Значение"),
        ("Всего прогнозов", len(predictions)),
        ("В наблюдении", sum(row.get("status") == "observing" for row in predictions)),
        ("Завершено", len(completed)),
        ("Памп подтверждён", len(successful)),
        ("Не подтвердился", len(completed) - len(successful)),
        ("Точность, %", round(len(successful) / len(completed) * 100, 2) if completed else 0),
    ]
    headers = (
        "ID", "Монета", "Score", "Стадия", "Статус", "Результат",
        "Стартовая цена", "Текущая цена", "Макс. цена", "Макс. рост, %",
        "Мин. цена", "Макс. просадка, %", "Новостной Score", "Новостей",
        "Критическая новость", "Причины", "Обнаружено", "Завершено",
    )
    prediction_rows = [headers]
    checkpoint_rows = [("ID", "Монета", "5 мин", "15 мин", "30 мин", "1 ч", "3 ч", "6 ч", "12 ч", "24 ч")]
    for row in predictions:
        try:
            technical = json.loads(row.get("technical_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            technical = {}
        try:
            checkpoints = json.loads(row.get("checkpoints_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            checkpoints = {}
        start = row.get("start_price")
        prediction_rows.append((
            row.get("id"), row.get("symbol"), row.get("score"), row.get("stage"),
            row.get("status"), row.get("outcome"), start, row.get("current_price"),
            row.get("max_price"), _percent(row.get("max_price"), start),
            row.get("min_price"), _percent(row.get("min_price"), start),
            row.get("news_score"), row.get("news_items"),
            "Да" if row.get("news_critical") else "Нет",
            ", ".join(technical.get("reasons", [])), row.get("detected_at"),
            row.get("completed_at"),
        ))
        checkpoint_rows.append((
            row.get("id"), row.get("symbol"),
            *(checkpoints.get(key) for key in CHECKPOINTS),
        ))

    sheets = (
        ("Сводка", summary),
        ("Прогнозы", prediction_rows),
        ("Контрольные точки", checkpoint_rows),
    )
    sheet_nodes = "".join(
        f'<sheet name="{escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, (name, _) in enumerate(sheets, 1)
    )
    relationships = "".join(
        f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, len(sheets) + 1)
    )
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, len(sheets) + 1)
    )
    files = {
        "[Content_Types].xml": f'''<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>{overrides}</Types>''',
        "_rels/.rels": '''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>''',
        "xl/workbook.xml": f'''<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>{sheet_nodes}</sheets></workbook>''',
        "xl/_rels/workbook.xml.rels": f'''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{relationships}<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>''',
        "xl/styles.xml": '''<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font/><font><b/><color rgb="FFFFFFFF"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="2"><xf/><xf fontId="1" fillId="1" applyFont="1" applyFill="1"/></cellXfs></styleSheet>''',
    }
    for index, (_, rows) in enumerate(sheets, 1):
        files[f"xl/worksheets/sheet{index}.xml"] = _worksheet(rows)
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()
