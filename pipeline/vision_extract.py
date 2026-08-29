#!/usr/bin/env python3
"""Извлечение результатов квиза с фотографий через vision-модель (OpenAI API).

Читает data/raw/candidates.json (или все картинки из data/media/ при
--all-media), для каждого изображения запрашивает vision-модель и сохраняет
черновик JSON в data/drafts/<имя-картинки>.json. Существующие черновики
пропускаются (перезапись — флаг --force).

Настройка через .env: OPENAI_API_KEY (обязателен), OPENAI_MODEL
(по умолчанию gpt-4o-mini), OPENAI_BASE_URL (по умолчанию api.openai.com).
"""

import argparse
import base64
import json
import mimetypes
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = PROJECT_ROOT / "data" / "raw"
MEDIA_DIR = PROJECT_ROOT / "data" / "media"
DRAFTS_DIR = PROJECT_ROOT / "data" / "drafts"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

PROMPT = (
    "Это фото результатов квиза/интеллектуальной игры. "
    "Извлеки таблицу результатов в JSON: "
    '{"game_date": "YYYY-MM-DD или null", "game_title": "...", "venue": "...", '
    '"teams": [{"name": "...", "score": число, "place": число}]}. '
    'Если это не результаты квиза — верни {"is_results": false}.'
)


def encode_image(path):
    """Encode an image file as a base64 data URL."""
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


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
    return json.loads(content)


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
        description="Извлечение результатов квиза с фото через vision API"
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

        draft = {"source_file": path.name, **meta, "extracted": result}
        with open(draft_path, "w", encoding="utf-8") as f:
            json.dump(draft, f, ensure_ascii=False, indent=2)
        done += 1
        print(f"  Сохранено: {draft_path.name}")

    print(f"Готово: {done}, пропущено: {skipped}, ошибок: {failed}")
    print(f"Черновики: {DRAFTS_DIR}")


if __name__ == "__main__":
    main()
