import asyncio

from services.statistics_export_service import StatisticsExportService


exporter = StatisticsExportService()


async def export_statistics(update, context):
    try:
        workbook = await asyncio.to_thread(exporter.build_xlsx)
        message = (
            update.callback_query.message
            if update.callback_query
            else update.message
        )
        await message.reply_document(
            document=workbook,
            filename=exporter.filename(),
            caption=(
                "📊 Статистика торговли: сводка, сделки, сигналы "
                "и причины отклонений."
            ),
        )
    except Exception as error:
        message = (
            update.callback_query.message
            if update.callback_query
            else update.message
        )
        await message.reply_text(f"❌ Ошибка экспорта статистики:\n{error}")
