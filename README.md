# BrainBoy — рейтинг квиз-команды

Пайплайн для анализа экспорта чата WhatsApp квиз-команды: извлечение результатов
игр с фотографий через vision-модель и статическая веб-страница с рейтингом.

## Структура проекта

```
pipeline/
  parse_whatsapp.py    # парсер экспорта WhatsApp → data/raw/messages.json
  extract_images.py    # отбор картинок-кандидатов → data/media/, data/raw/candidates.json
  vision_extract.py    # извлечение результатов с фото через vision API → data/drafts/
  build_data.py        # сборка web/data.json из курируемого data/results.json
data/
  raw/                 # экспорт чата и промежуточные JSON (не коммитится)
  media/               # отобранные фото результатов (не коммитится)
  drafts/              # черновики авто-извлечения (не коммитятся)
  results.json         # курируемые вручную результаты игр (коммитится)
web/
  index.html           # статическая страница рейтинга
  data.json            # сгенерированные данные для страницы (коммитится)
```

## Как пользоваться

1. **Экспорт чата из WhatsApp.** В WhatsApp: чат → Ещё → Экспорт чата →
   «С файлами». Полученный zip положите в удобное место (в проект — можно,
   `*.zip` игнорируется git).

2. **Парсинг чата:**

   ```bash
   python3 pipeline/parse_whatsapp.py путь/к/export.zip
   ```

   Поддерживаются оба формата экспорта (iOS и Android). Результат —
   `data/raw/messages.json`.

3. **Отбор картинок-кандидатов** (по ключевым словам в тексте рядом с фото,
   либо все картинки с флагом `--all`):

   ```bash
   python3 pipeline/extract_images.py
   ```

   Кандидаты копируются в `data/media/`, список — `data/raw/candidates.json`.

4. **Извлечение результатов с фото через vision-модель.** Сначала настройте
   окружение:

   ```bash
   cp .env.example .env   # и впишите OPENAI_API_KEY
   pip install -r requirements.txt
   ```

   Затем:

   ```bash
   python3 pipeline/vision_extract.py
   ```

   Черновики складываются в `data/drafts/<имя-картинки>.json`. Уже обработанные
   картинки пропускаются (перезапись — `--force`).

5. **Ручная курация.** Просмотрите черновики в `data/drafts/`, проверьте их по
   фото и перенесите подтверждённые игры в `data/results.json`:

   ```json
   {
     "team_aliases": {"брейн бой": "BrainBoy"},
     "games": [
       {
         "date": "2024-08-25",
         "title": "Квиз, плиз!",
         "venue": "Бар «Улей»",
         "teams": [
           {"name": "BrainBoy", "score": 42, "place": 1}
         ]
       }
     ]
   }
   ```

   `team_aliases` приводит вариации названий команд к каноническому имени.

6. **Сборка данных для страницы:**

   ```bash
   python3 pipeline/build_data.py
   ```

   Генерирует `web/data.json` со стендингом и историей игр.

7. **Локальный просмотр:**

   ```bash
   cd web && python3 -m http.server 8000
   ```

   Откройте http://localhost:8000

8. **Деплой на Vercel.** Проект настроен как чистая статика из папки `web/`
   (см. `vercel.json`). Достаточно подключить репозиторий к Vercel или выполнить
   `vercel deploy`.

## Приватность

Экспорт чата содержит личную переписку и номера телефонов, поэтому
`data/raw/`, `data/media/`, `data/drafts/`, `*.zip` и `_chat.txt` игнорируются
git и никогда не коммитятся. Коммитятся только курируемый `data/results.json`
и сгенерированный `web/data.json` — следите, чтобы туда не попадало ничего
лишнего. Ключ API хранится в `.env` (тоже игнорируется); шаблон — `.env.example`.
