from io import BytesIO
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile


HEADERS = (
    "ID", "Монета", "Score", "Статус", "Цена входа", "Текущая цена",
    "Цена выхода", "Текущий результат, %", "Макс. результат, %",
    "Сумма, USDT", "Количество", "Результат, USDT",
    "Причина закрытия", "Цель +3%",
    "Макс. цена", "Мин. цена", "Дата входа", "Дата закрытия",
)


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
    return (f'<c r="{reference}" t="inlineStr"{style}><is><t>'
            f'{escape(str(value))}</t></is></c>')


def build_trades_xlsx(trades: list[dict]) -> bytes:
    rows = [HEADERS]
    for trade in trades:
        close = trade.get("close_price")
        current = close or trade.get("current_price") or trade["entry_price"]
        current_result = round((current / trade["entry_price"] - 1) * 100, 4)
        max_result = round(
            (trade["max_price"] / trade["entry_price"] - 1) * 100, 4
        )
        position = trade.get("position_usdt")
        pnl_usdt = round(position * current_result / 100, 6) if position else None
        rows.append((
            trade["id"], trade["symbol"], trade["score"], trade["status"],
            trade["entry_price"], current, close, current_result, max_result,
            position, trade.get("quantity"), pnl_usdt, trade.get("close_reason"),
            trade["tp1"], trade["max_price"], trade["min_price"],
            trade["opened_at"], trade.get("closed_at"),
        ))
    xml_rows = []
    for row_number, row in enumerate(rows, 1):
        cells = "".join(_cell(f"{_column(column)}{row_number}", value,
                              row_number == 1)
                        for column, value in enumerate(row, 1))
        xml_rows.append(f'<row r="{row_number}">{cells}</row>')
    last_cell = f"R{len(rows)}"
    sheet = (f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<dimension ref="A1:{last_cell}"/><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" state="frozen"/></sheetView></sheetViews>
<sheetData>{''.join(xml_rows)}</sheetData><autoFilter ref="A1:{last_cell}"/></worksheet>''')
    files = {
        "[Content_Types].xml": '''<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>''',
        "_rels/.rels": '''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>''',
        "xl/workbook.xml": '''<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Сделки" sheetId="1" r:id="rId1"/></sheets></workbook>''',
        "xl/_rels/workbook.xml.rels": '''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>''',
        "xl/styles.xml": '''<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font/><font><b/><color rgb="FFFFFFFF"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="2"><xf/><xf fontId="1" fillId="1" applyFont="1" applyFill="1"/></cellXfs></styleSheet>''',
        "xl/worksheets/sheet1.xml": sheet,
    }
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()
