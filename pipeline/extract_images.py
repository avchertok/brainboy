#!/usr/bin/env python3
"""Отбор изображений-кандидатов с результатами игры.

Читает data/raw/messages.json, находит сообщения с вложениями-картинками,
фильтрует по ключевым словам в тексте сообщения и соседних сообщений
(или берёт все картинки при --all), копирует кандидатов в data/media/
с префиксом даты и пишет data/raw/candidates.json.

Если экспорт был сделан БЕЗ медиафайлов (в чате есть записи о вложениях,
но самих файлов нет), скрипт не падает, а печатает агрегированный отчёт:
сколько фото приходится на каждое воскресенье (игры проходят по
воскресеньям) — и сохраняет его в data/raw/media_report.json. После
повторного экспорта «с файлами» этот отчёт помогает быстро сматчить
фотографии с датами игр.

Приватность: в stdout выводятся только агрегаты (числа, даты, имена
файлов вложений) — никогда не тексты сообщений и не имена авторов.
"""

import argparse
import json
import shutil
import sys
from collections import Counter
from datetime import datetime, timedelta
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


def nearest_past_sunday(d):
    """Return the nearest Sunday on or before the given date."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


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


def message_date(msg):
    """Parse the message timestamp to a date, or None."""
    ts = msg.get("timestamp")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).date()
    except ValueError:
        return None


def any_media_present(messages, export_dir):
    """Check whether at least one named attachment file exists in the export."""
    for msg in messages:
        for a in msg.get("attachments", []):
            if a is not None and (export_dir / a).exists():
                return True
    return False


def report_missing_media(messages):
    """Aggregate image attachments by nearest past Sunday (no message bodies).

    Пишет data/raw/media_report.json и печатает только агрегаты:
    количества фото по воскресеньям и по годам.
    """
    # Учитываем и картинки с именами, и "<Media omitted>" (attachments=[None])
    by_sunday = Counter()
    named_images = 0
    omitted = 0
    for msg in messages:
        attachments = msg.get("attachments", [])
        images = [a for a in attachments if is_image(a)]
        omitted_here = sum(1 for a in attachments if a is None)
        if not images and not omitted_here:
            continue
        named_images += len(images)
        omitted += omitted_here
        d = message_date(msg)
        if d is None:
            continue
        sunday = nearest_past_sunday(d).isoformat()
        by_sunday[sunday] += len(images) + omitted_here

    report = {
        "note": (
            "Экспорт без медиафайлов: файлов вложений нет, только записи о них. "
            "Кандидаты по воскресеньям (ближайшее прошедшее воскресенье от даты "
            "сообщения) — для матчинга после повторного экспорта с файлами."
        ),
        "images_with_filenames": named_images,
        "media_omitted": omitted,
        "sundays": dict(sorted(by_sunday.items())),
    }
    out_path = RAW_DIR / "media_report.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    by_year = Counter()
    for sunday, count in by_sunday.items():
        by_year[sunday[:4]] += count

    print()
    print("В экспорте нет медиафайлов (экспорт сделан «без файлов»).")
    print("Сделайте повторный экспорт чата в WhatsApp с опцией «Прикрепить файлы»")
    print("(Настройки чата → Экспорт чата → «Прикрепить файлы») — см. README.")
    print()
    print(f"Вложений-картинок с именами файлов: {named_images}")
    print(f"Вложений без имени файла (<Media omitted>): {omitted}")
    print(f"Воскресений с фото (кандидаты на даты игр): {len(by_sunday)}")
    print()
    print("Фото по годам:")
    for year, count in sorted(by_year.items()):
        print(f"  {year}: {count}")
    print()
    print(f"Полный отчёт по воскресеньям: {out_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Отбор картинок-кандидатов с результатами игры"
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
        print(f"Пропущено из-за отсутствия файла в экспорте: {skipped_missing}")
    print(f"Список: {out_path}, файлы: {MEDIA_DIR}")

    has_attachment_records = any(msg.get("attachments") for msg in messages)
    if not candidates and has_attachment_records and not any_media_present(
        messages, export_dir
    ):
        # Записи о вложениях есть, файлов нет — экспорт без медиа
        report_missing_media(messages)


if __name__ == "__main__":
    main()
