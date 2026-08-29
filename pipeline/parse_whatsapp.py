#!/usr/bin/env python3
"""Парсер экспорта чата WhatsApp (iOS и Android) в data/raw/messages.json.

Вход: путь к .zip экспорта, к папке с _chat.txt или напрямую к txt-файлу.
Выход: data/raw/messages.json вида
    {"export_dir": "...", "format": "ios|android", "messages": [
        {"timestamp": ISO, "author": str|null, "text": str, "attachments": [str]}
    ]}
Медиафайлы никуда не копируются — только фиксируются имена вложений
и путь к директории экспорта (копирование делает extract_images.py).
"""

import argparse
import json
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"

LRM = "\u200e"

# iOS: [25.08.2024, 21:14:33] Author: text  (дата бывает DD.MM.YYYY или MM/DD/YY,
# секунды опциональны, время бывает 12-часовое с AM/PM, в начале строки возможен LRM)
IOS_RE = re.compile(
    r"^\u200e?\[(?P<date>[\d./]+),\s*"
    r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?(?:\s?[APap][Mm])?)\]\s*"
    r"(?P<author>[^:]+?):\s?(?P<text>.*)$"
)

# Android: 25.08.2024, 21:14 - Author: text  (у системных сообщений нет "Author: ")
ANDROID_RE = re.compile(
    r"^\u200e?(?P<date>[\d./]+),\s*(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\s+-\s+(?P<rest>.*)$"
)

# Вложения
IOS_ATTACH_RE = re.compile(r"\u200e?<attached:\s*(?P<file>[^>]+)>")
ANDROID_ATTACH_RE = re.compile(
    r"(?P<file>\S+)\s+\((?:file attached|файл добавлен)\)", re.IGNORECASE
)
# "<Media omitted>" (Android), "<image omitted>" / "<video omitted>" и т.п. (iOS)
MEDIA_OMITTED_RE = re.compile(
    r"<\u200e?(?:Media|image|video|audio|sticker|GIF|document|Contact card) omitted>"
    r"|<Без медиафайлов>",
    re.IGNORECASE,
)

DATE_FORMATS = ["%d.%m.%Y", "%d.%m.%y", "%m/%d/%y", "%m/%d/%Y", "%d/%m/%Y", "%d/%m/%y"]
DAY_FIRST_FORMATS = ["%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%d/%m/%y", "%m/%d/%y", "%m/%d/%Y"]
MONTH_FIRST_FORMATS = ["%m/%d/%y", "%m/%d/%Y", "%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%d/%m/%y"]

DATE_TOKEN_RE = re.compile(r"^\u200e?\[?(\d{1,2})[./](\d{1,2})[./]\d{2,4},")


def detect_date_formats(chat_path):
    """Scan all date tokens to disambiguate day-first vs month-first ordering.

    Слэш-даты неоднозначны (12/07 = 12 июля или 7 декабря): смотрим на весь
    файл — если первый компонент бывает >12, это день; если второй — месяц.
    """
    first_gt12 = second_gt12 = False
    with open(chat_path, encoding="utf-8") as f:
        for line in f:
            m = DATE_TOKEN_RE.match(line)
            if m:
                if int(m.group(1)) > 12:
                    first_gt12 = True
                if int(m.group(2)) > 12:
                    second_gt12 = True
            if first_gt12 and second_gt12:
                break
    if first_gt12 and not second_gt12:
        return DAY_FIRST_FORMATS
    if second_gt12 and not first_gt12:
        return MONTH_FIRST_FORMATS
    return DATE_FORMATS


def parse_timestamp(date_str, time_str, date_formats=DATE_FORMATS):
    """Return ISO timestamp or None if the date/time cannot be parsed."""
    time_str = time_str.strip()
    has_ampm = time_str[-1] in "mM"
    hour_fmt = "%I" if has_ampm else "%H"
    seconds = ":%S" if time_str.count(":") == 2 else ""
    ampm = " %p" if has_ampm else ""
    time_fmt = f"{hour_fmt}:%M{seconds}{ampm}"
    time_str = re.sub(r"\s*([APap][Mm])$", r" \1", time_str).upper()
    for date_fmt in date_formats:
        try:
            dt = datetime.strptime(f"{date_str} {time_str}", f"{date_fmt} {time_fmt}")
            return dt.isoformat()
        except ValueError:
            continue
    return None


def extract_attachments(text):
    """Return (clean_text, attachment_filenames) for a message body."""
    attachments = []

    def take_ios(m):
        attachments.append(m.group("file").strip())
        return ""

    def take_android(m):
        attachments.append(m.group("file").strip())
        return ""

    text = IOS_ATTACH_RE.sub(take_ios, text)
    text = ANDROID_ATTACH_RE.sub(take_android, text)
    if MEDIA_OMITTED_RE.search(text):
        # Вложение было, но файл не включён в экспорт
        text = MEDIA_OMITTED_RE.sub("", text)
        attachments.append(None)
    return text.strip().strip(LRM), attachments


def parse_line(line, date_formats=DATE_FORMATS):
    """Try to parse a message header line. Returns (fmt, msg_dict) or (None, None)."""
    m = IOS_RE.match(line)
    if m:
        ts = parse_timestamp(m.group("date"), m.group("time"), date_formats)
        if ts:
            return "ios", {
                "timestamp": ts,
                "author": m.group("author").strip().strip(LRM),
                "text": m.group("text"),
            }
    m = ANDROID_RE.match(line)
    if m:
        ts = parse_timestamp(m.group("date"), m.group("time"), date_formats)
        if ts:
            rest = m.group("rest")
            author, sep, text = rest.partition(": ")
            if sep:
                return "android", {
                    "timestamp": ts,
                    "author": author.strip().strip(LRM),
                    "text": text,
                }
            # Системное сообщение без автора
            return "android", {"timestamp": ts, "author": None, "text": rest}
    return None, None


def parse_chat(chat_path):
    """Parse a _chat.txt file into (format, messages)."""
    raw_messages = []
    detected = {}
    date_formats = detect_date_formats(chat_path)
    with open(chat_path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            fmt, msg = parse_line(line, date_formats)
            if msg:
                detected[fmt] = detected.get(fmt, 0) + 1
                raw_messages.append(msg)
            elif raw_messages:
                # Продолжение многострочного сообщения
                raw_messages[-1]["text"] += "\n" + line

    messages = []
    for msg in raw_messages:
        text, attachments = extract_attachments(msg["text"])
        # None в attachments = "<Media omitted>": вложение было, файла нет
        messages.append(
            {
                "timestamp": msg["timestamp"],
                "author": msg["author"],
                "text": text,
                "attachments": attachments,
            }
        )
    fmt = max(detected, key=detected.get) if detected else "unknown"
    return fmt, messages


def resolve_input(input_path):
    """Resolve input (zip / dir / txt) to (chat_txt_path, export_dir)."""
    path = Path(input_path)
    if not path.exists():
        sys.exit(f"Ошибка: путь не найден: {path}")

    if path.is_file() and path.suffix.lower() == ".zip":
        extract_dir = RAW_DIR / path.stem
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path) as zf:
            zf.extractall(extract_dir)
        print(f"Распакован zip в {extract_dir}")
        path = extract_dir

    if path.is_dir():
        candidates = sorted(path.glob("*_chat*.txt")) or sorted(path.glob("*.txt"))
        if not candidates:
            sys.exit(f"Ошибка: в {path} не найден txt-файл с чатом")
        return candidates[0], path

    return path, path.parent


def main():
    parser = argparse.ArgumentParser(
        description="Парсер экспорта WhatsApp в data/raw/messages.json"
    )
    parser.add_argument(
        "input", help="Путь к .zip экспорта, папке с _chat.txt или самому txt-файлу"
    )
    parser.add_argument(
        "-o",
        "--output",
        default=str(RAW_DIR / "messages.json"),
        help="Куда писать JSON (по умолчанию data/raw/messages.json)",
    )
    args = parser.parse_args()

    chat_path, export_dir = resolve_input(args.input)
    print(f"Читаю чат: {chat_path}")
    fmt, messages = parse_chat(chat_path)

    output = {
        "export_dir": str(export_dir.resolve()),
        "format": fmt,
        "messages": messages,
    }
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    with_attachments = sum(1 for m in messages if m["attachments"])
    print(f"Формат экспорта: {fmt}")
    print(f"Всего сообщений: {len(messages)}")
    print(f"Сообщений с вложениями: {with_attachments}")
    print(f"Результат: {out_path}")


if __name__ == "__main__":
    main()
