import io
import math
import re
import zipfile
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from database.db import Database
from services.demo_statistics_service import DemoStatisticsService


class StatisticsExportService:
    """Create a dependency-free Excel workbook for strategy review."""

    def __init__(self, database=None):
        self.database = database or Database()

    def build_xlsx(self):
        sheets = [
            ("Сводка", self._summary_rows()),
            ("Сделки", self._table_rows("demo_trades")),
            ("Сигналы", self._table_rows("signal_logs")),
            ("Настройки", self._table_rows("app_settings")),
        ]
        output = io.BytesIO()
        with zipfile.ZipFile(
            output, "w", compression=zipfile.ZIP_DEFLATED
        ) as workbook:
            self._write_package(workbook, sheets)
        output.seek(0)
        return output

    def filename(self):
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M")
        return f"trading_statistics_{stamp}.xlsx"

    def _summary_rows(self):
        stats = DemoStatisticsService().get_statistics()
        with self.database.connect() as connection:
            signal_count = self._count(connection, "signal_logs")
            approved_count = self._count(
                connection, "signal_logs", "final_approved = 1"
            )
            opened_count = self._count(
                connection,
                "signal_logs",
                "execution_status = 'opened'",
            )
            failed_count = self._count(
                connection,
                "signal_logs",
                "execution_status = 'failed'",
            )
        labels = {
            "total_trades": "Всего сделок",
            "open_trades": "Открытых сделок",
            "closed_trades": "Закрытых сделок",
            "winning_trades": "Прибыльных",
            "losing_trades": "Убыточных",
            "win_rate": "Win rate, %",
            "total_pnl": "Общий PnL, USDT",
            "average_pnl": "Средний PnL, USDT",
            "maximum_drawdown": "Максимальная просадка, USDT",
            "best_trade": "Лучшая сделка, USDT",
            "worst_trade": "Худшая сделка, USDT",
        }
        rows = [["Показатель", "Значение"]]
        rows.extend([labels[key], stats[key]] for key in labels)
        rows.extend([
            ["Проверено сигналов", signal_count],
            ["Одобрено сигналов", approved_count],
            ["Успешно открыто по сигналам", opened_count],
            ["Ошибок исполнения", failed_count],
            [
                "Доля одобренных, %",
                round(approved_count / signal_count * 100, 2)
                if signal_count else 0,
            ],
            [
                "Дата экспорта UTC",
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ],
        ])
        return rows

    def _table_rows(self, table):
        with self.database.connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            if not exists:
                return [["Нет данных"]]
            cursor = connection.execute(f'SELECT * FROM "{table}" ORDER BY 1')
            headers = [item[0] for item in cursor.description]
            return [headers, *cursor.fetchall()]

    @staticmethod
    def _count(connection, table, where=None):
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if not exists:
            return 0
        clause = f" WHERE {where}" if where else ""
        return connection.execute(
            f'SELECT COUNT(*) FROM "{table}"{clause}'
        ).fetchone()[0]

    def _write_package(self, workbook, sheets):
        workbook.writestr(
            "[Content_Types].xml", self._content_types(len(sheets))
        )
        workbook.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/'
            'package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.'
            'org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>',
        )
        workbook.writestr("xl/workbook.xml", self._workbook_xml(sheets))
        workbook.writestr(
            "xl/_rels/workbook.xml.rels",
            self._workbook_relationships(len(sheets)),
        )
        workbook.writestr("xl/styles.xml", self._styles_xml())
        for index, (_, rows) in enumerate(sheets, start=1):
            workbook.writestr(
                f"xl/worksheets/sheet{index}.xml",
                self._sheet_xml(rows),
            )

    @staticmethod
    def _sheet_xml(rows):
        row_xml = []
        for row_index, row in enumerate(rows, start=1):
            cells = []
            for column_index, value in enumerate(row, start=1):
                reference = (
                    StatisticsExportService._column_name(column_index)
                    + str(row_index)
                )
                style = ' s="1"' if row_index == 1 else ""
                if (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(float(value))
                ):
                    cells.append(
                        f'<c r="{reference}"{style}><v>{value}</v></c>'
                    )
                else:
                    text = "" if value is None else str(value)
                    text = re.sub(
                        r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", text
                    )
                    cells.append(
                        f'<c r="{reference}" t="inlineStr"{style}>'
                        f"<is><t>{escape(text)}</t></is></c>"
                    )
            row_xml.append(
                f'<row r="{row_index}">{"".join(cells)}</row>'
            )
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main"><sheetData>'
            + "".join(row_xml)
            + "</sheetData></worksheet>"
        )

    @staticmethod
    def _column_name(index):
        name = ""
        while index:
            index, remainder = divmod(index - 1, 26)
            name = chr(65 + remainder) + name
        return name

    @staticmethod
    def _workbook_xml(sheets):
        entries = "".join(
            f'<sheet name="{escape(name)}" sheetId="{index}" '
            f'r:id="rId{index}"/>'
            for index, (name, _) in enumerate(sheets, start=1)
        )
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main" xmlns:r="http://schemas.'
            'openxmlformats.org/officeDocument/2006/relationships">'
            f"<sheets>{entries}</sheets></workbook>"
        )

    @staticmethod
    def _workbook_relationships(count):
        entries = "".join(
            '<Relationship '
            f'Id="rId{index}" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
            for index in range(1, count + 1)
        )
        entries += (
            '<Relationship Id="rIdStyles" Type="http://schemas.'
            'openxmlformats.org/officeDocument/2006/relationships/styles" '
            'Target="styles.xml"/>'
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/'
            f'package/2006/relationships">{entries}</Relationships>'
        )

    @staticmethod
    def _content_types(count):
        sheets = "".join(
            '<Override '
            f'PartName="/xl/worksheets/sheet{index}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'spreadsheetml.worksheet+xml"/>'
            for index in range(1, count + 1)
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
            'content-types"><Default Extension="rels" ContentType="'
            'application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            f"{sheets}</Types>"
        )

    @staticmethod
    def _styles_xml():
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main"><fonts count="2"><font/>'
            '<font><b/></font></fonts><fills count="1"><fill><patternFill '
            'patternType="none"/></fill></fills><borders count="1"><border/>'
            '</borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" '
            'fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2">'
            '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" '
            'applyFont="1"/>'
            '</cellXfs></styleSheet>'
        )
