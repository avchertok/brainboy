#!/usr/bin/env python3
"""Извлечение результатов игры BrainBoy с фотографий через vision-модель.

Читает data/raw/candidates.json (или все картинки из data/media/ при
--all-media), для каждого изображения запрашивает vision-модель и сохраняет
черновик JSON в data/drafts/<имя-картинки>.json. Существующие черновики
пропускаются (перезапись — флаг --force).

Черновик содержит:
  - extracted: таблица результатов (8 раундов по столбцам, итог, место);
  - candidate_dates: {"exif": ..., "message": ..., "nearest_sunday": ...} —
    варианты даты игры (EXIF DateTimeOriginal фото, дата сообщения в чате,
    ближайшее прошедшее воскресенье от даты сообщения — игры по воскресеньям).

Консолидация черновиков в data/results.json — pipeline/merge_drafts.py.

Настройка через .env: OPENAI_API_KEY (обязателен), OPENAI_MODEL
(по умолчанию gpt-4o-mini), OPENAI_BASE_URL (по умолчанию api.openai.com).
"""

import argparse
import base64
import json
import mimetypes
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image, ExifTags

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
MEDIA_DIR = PROJECT_ROOT / "data" / "media"
DRAFTS_DIR = PROJECT_ROOT / "data" / "drafts"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

ROUNDS_COUNT = 8

PROMPT = (
    "Это фотография таблицы результатов интеллектуальной игры BrainBoy. "
    f"В игре {ROUNDS_COUNT} раундов, последний ({ROUNDS_COUNT}-й) — «капитанский конкурс». "
    "В таблице обычно есть: место команды, название команды, "
    f"очки за каждый из {ROUNDS_COUNT} раундов (по столбцам) и итоговая сумма. "
    "Извлеки таблицу в JSON строго такой структуры:\n"
    "{\n"
    '  "is_results": true,\n'
    '  "game_date": "YYYY-MM-DD, если дата видна на фото, иначе null",\n'
    '  "game_title": "название игры/пакета, если видно, иначе null",\n'
    '  "venue": "место проведения, если видно, иначе null",\n'
    '  "teams": [\n'
    '    {"name": "название команды точно как на фото",\n'
    '     "place": число (место) или null,\n'
    f'     "rounds": [ровно {ROUNDS_COUNT} чисел — очки по раундам по порядку; '
    "null для нечитаемых или отсутствующих ячеек],\n"
    '     "total": итоговая сумма очков (число) или null}\n'
    "  ]\n"
    "}\n"
    "Названия команд переписывай посимвольно, не исправляй и не переводи. "
    'Если на фото не таблица результатов игры — верни {"is_results": false}.'
)

EXIF_DATETIME_ORIGINAL = next(
    k for k, v in ExifTags.TAGS.items() if v == "DateTimeOriginal"
)


def nearest_past_sunday(d):
    """Return the nearest Sunday on or before the given date."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


def exif_date(image_path):
    """Return the EXIF DateTimeOriginal date (ISO string) or None."""
    try:
        with Image.open(image_path) as img:
            exif = img.getexif()
            value = exif.get(EXIF_DATETIME_ORIGINAL)
            if not value:
                # DateTimeOriginal живёт в EXIF IFD; getexif() отдаёт базовый IFD
                ifd = exif.get_ifd(0x8769)
                value = ifd.get(EXIF_DATETIME_ORIGINAL)
    except Exception:
        return None
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y:%m:%d %H:%M:%S").date().isoformat()
    except ValueError:
        return None


def candidate_dates(image_path, meta):
    """Build the candidate_dates block for a draft.

    exif           — дата съёмки фото из EXIF (если есть);
    message        — дата сообщения в чате, к которому было приложено фото;
    nearest_sunday — ближайшее прошедшее воскресенье от даты сообщения
                     (игры проходят по воскресеньям).
    """
    message_date = None
    ts = meta.get("timestamp")
    if ts:
        try:
            message_date = datetime.fromisoformat(ts).date()
        except ValueError:
            pass
    return {
        "exif": exif_date(image_path),
        "message": message_date.isoformat() if message_date else None,
        "nearest_sunday": (
            nearest_past_sunday(message_date).isoformat() if message_date else None
        ),
    }


def encode_image(path):
    """Encode an image file as a base64 data URL."""
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def normalize_extracted(result):
    """Pad/trim rounds arrays to exactly ROUNDS_COUNT items."""
    for team in result.get("teams") or []:
        rounds = team.get("rounds")
        if not isinstance(rounds, list):
            rounds = []
        rounds = rounds[:ROUNDS_COUNT] + [None] * (ROUNDS_COUNT - len(rounds))
        team["rounds"] = rounds
    return result


def extract_one(client, model, image_path):
    """Send one image to the vision model, return parsed JSON dict."""
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {
                    "type": "image_url",
                    "image_url": {"url": encode_image(image_path)},
                },
            ],
        }
    ]
    try:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
        )
    except Exception:
        # Некоторые OpenAI-совместимые API не поддерживают response_format
        response = client.chat.completions.create(model=model, messages=messages)

    content = response.choices[0].message.content or ""
    # На случай, если модель обернула JSON в markdown-блок
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:]
    return normalize_extracted(json.loads(content))


def collect_images(all_media):
    """Return list of (path, meta) images to process."""
    if all_media:
        files = sorted(
            p for p in MEDIA_DIR.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS
        )
        return [(p, {}) for p in files]

    candidates_path = RAW_DIR / "candidates.json"
    if not candidates_path.exists():
        sys.exit(
            f"Ошибка: {candidates_path} не найден. "
            "Запустите extract_images.py или используйте --all-media"
        )
    with open(candidates_path, encoding="utf-8") as f:
        candidates = json.load(f)
    result = []
    for c in candidates:
        path = MEDIA_DIR / c["file"]
        if path.exists():
            result.append((path, c))
        else:
            print(f"  Пропуск (файл не найден): {c['file']}")
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Извлечение результатов игры с фото через vision API"
    )
    parser.add_argument(
        "--all-media",
        action="store_true",
        help="Обработать все картинки из data/media/, игнорируя candidates.json",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Перезаписывать уже существующие черновики",
    )
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        sys.exit(
            "Ошибка: не задан OPENAI_API_KEY. "
            "Скопируйте .env.example в .env и укажите ключ."
        )
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    client = OpenAI(api_key=api_key, base_url=base_url)

    images = collect_images(args.all_media)
    if not images:
        print("Нет изображений для обработки.")
        return

    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    done = skipped = failed = 0

    for i, (path, meta) in enumerate(images, 1):
        draft_path = DRAFTS_DIR / f"{path.name}.json"
        if draft_path.exists() and not args.force:
            skipped += 1
            print(f"[{i}/{len(images)}] Пропуск (черновик уже есть): {path.name}")
            continue

        print(f"[{i}/{len(images)}] Обработка: {path.name} ...")
        try:
            result = extract_one(client, model, path)
        except Exception as e:
            failed += 1
            print(f"  Ошибка: {e}")
            continue

        draft = {
            "source_file": path.name,
            **meta,
            "candidate_dates": candidate_dates(path, meta),
            "extracted": result,
        }
        with open(draft_path, "w", encoding="utf-8") as f:
            json.dump(draft, f, ensure_ascii=False, indent=2)
        done += 1
        print(f"  Сохранено: {draft_path.name}")

    print(f"Готово: {done}, пропущено: {skipped}, ошибок: {failed}")
    print(f"Черновики: {DRAFTS_DIR}")
    print("Следующий шаг: python3 pipeline/merge_drafts.py (отчёт о черновиках)")


if __name__ == "__main__":
    main()
