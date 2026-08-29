#!/usr/bin/env python3
"""Отбор изображений-кандидатов с результатами квиза.

Читает data/raw/messages.json, находит сообщения с вложениями-картинками,
фильтрует по ключевым словам в тексте сообщения и соседних сообщений
(или берёт все картинки при --all), копирует кандидатов в data/media/
с префиксом даты и пишет data/raw/candidates.json.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
MEDIA_DIR = PROJECT_ROOT / "data" / "media"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

DEFAULT_KEYWORDS = [
    "место",
    "балл",
    "очк",
    "результат",
    "итог",
    "тур",
    "квиз",
    "таблиц",
    "рейтинг",
    "счет",
    "счёт",
]


def is_image(filename):
    # None = "<Media omitted>": вложение без файла в экспорте
    return filename is not None and Path(filename).suffix.lower() in IMAGE_EXTENSIONS


def context_matches(messages, index, keywords, window):
    """Check if the message or its neighbours contain any keyword.

    Returns the matched context text, or None.
    """
    lo = max(0, index - window)
    hi = min(len(messages), index + window + 1)
    context_parts = []
    matched = False
    for i in range(lo, hi):
        text = messages[i].get("text") or ""
        if text:
            context_parts.append(text)
        lowered = text.lower()
        if any(kw in lowered for kw in keywords):
            matched = True
    return " | ".join(context_parts) if matched else None


def main():
    parser = argparse.ArgumentParser(
        description="Отбор картинок-кандидатов с результатами квиза"
    )
    parser.add_argument(
        "--messages",
        default=str(RAW_DIR / "messages.json"),
        help="Путь к messages.json (по умолчанию data/raw/messages.json)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Брать все изображения без фильтра по ключевым словам",
    )
    parser.add_argument(
        "--keywords",
        default=",".join(DEFAULT_KEYWORDS),
        help="Список ключевых слов через запятую",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=1,
        help="Сколько соседних сообщений учитывать при поиске ключевых слов",
    )
    args = parser.parse_args()

    messages_path = Path(args.messages)
    if not messages_path.exists():
        sys.exit(
            f"Ошибка: {messages_path} не найден. Сначала запустите parse_whatsapp.py"
        )

    with open(messages_path, encoding="utf-8") as f:
        data = json.load(f)

    export_dir = Path(data.get("export_dir", "."))
    messages = data["messages"]
    keywords = [kw.strip().lower() for kw in args.keywords.split(",") if kw.strip()]

    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    candidates = []
    skipped_missing = 0

    for i, msg in enumerate(messages):
        images = [a for a in msg.get("attachments", []) if is_image(a)]
        if not images:
            continue

        if args.all:
            context = msg.get("text") or ""
        else:
            context = context_matches(messages, i, keywords, args.window)
            if context is None:
                continue

        date_prefix = (msg.get("timestamp") or "")[:10] or "unknown-date"
        for filename in images:
            src = export_dir / filename
            if not src.exists():
                skipped_missing += 1
                print(f"  Пропуск (файл не найден): {filename}")
                continue
            dest_name = f"{date_prefix}_{Path(filename).name}"
            dest = MEDIA_DIR / dest_name
            shutil.copy2(src, dest)
            candidates.append(
                {
                    "file": dest_name,
                    "timestamp": msg.get("timestamp"),
                    "author": msg.get("author"),
                    "context_text": context,
                }
            )
            print(f"  Кандидат: {dest_name}")

    out_path = RAW_DIR / "candidates.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(candidates, f, ensure_ascii=False, indent=2)

    print(f"Отобрано кандидатов: {len(candidates)}")
    if skipped_missing:
        print(f"Пропущено из-за отсутствия файла: {skipped_missing}")
    print(f"Список: {out_path}, файлы: {MEDIA_DIR}")


if __name__ == "__main__":
    main()
