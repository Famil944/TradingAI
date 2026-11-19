"""Safely enable/disable the early 30–40s research arm in a local .env.

This script never starts another bot process, logs secrets, reads API keys or
modifies core signal settings. The bot must be restarted to reload its .env.
"""
import argparse
import os
import re
import stat
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KEY = "OTC_EARLY_SHADOW_ENABLED"
ENTRY = re.compile(r"^\s*(?:export\s+)?" + KEY + r"\s*=")


def updated_env(original, enabled):
    """Set exactly one active assignment, preserve every other .env line."""
    lines = original.splitlines(keepends=True)
    setting = KEY + ("=true" if enabled else "=false")
    newline = "\r\n" if "\r\n" in original else "\n"
    replaced = False
    result = []
    for line in lines:
        if ENTRY.match(line.rstrip("\r\n")):
            if not replaced:
                result.append(setting + newline)
                replaced = True
            # Avoid ambiguous duplicate definitions.
            continue
        result.append(line)
    if not replaced:
        if original and not original.endswith(("\r", "\n")):
            result.append(newline)
        result.append(setting + newline)
    return "".join(result)


def configure(path, enabled):
    """Atomically edit .env only, retaining the original file mode."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Нельзя редактировать .env через символическую ссылку.")
    if not path.is_file():
        raise FileNotFoundError(
            f"Файл {path} не найден. Нужен существующий .env с ключами бота."
        )
    old = path.read_text(encoding="utf-8")
    new = updated_env(old, enabled)
    if old == new:
        return False
    mode = stat.S_IMODE(path.stat().st_mode)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent,
            prefix=".tradingai-env-", suffix=".tmp", delete=False,
        ) as out:
            temp = Path(out.name)
            out.write(new)
            out.flush()
            os.fsync(out.fileno())
        temp.chmod(mode)
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    toggle = parser.add_mutually_exclusive_group(required=True)
    toggle.add_argument("--enable", action="store_true",
                        help="Включить фоновый 30–40с анализ")
    toggle.add_argument("--disable", action="store_true",
                        help="Выключить фоновый 30–40с анализ")
    args = parser.parse_args(argv)
    target = ROOT / ".env"
    try:
        changed = configure(target, args.enable)
    except (OSError, UnicodeError, ValueError) as exc:
        parser.exit(1, f"Не удалось обновить .env: {exc}\n")
    state = "включён" if args.enable else "выключен"
    result = "настройка сохранена" if changed else "уже был установлен"
    print(f"✅ Фоновый тест 30–40 секунд {state}; {result}.")
    if KEY in os.environ:
        print("⚠️ Важно: переменная из окружения имеет приоритет перед .env.")
    print("Перезапустите единственный экземпляр TradingAI, чтобы применить настройку.")
    print("Основные S1–S4 и их время отправки не менялись.")


if __name__ == "__main__":
    main()
