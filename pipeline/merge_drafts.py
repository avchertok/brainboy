#!/usr/bin/env python3
"""Консолидация черновиков data/drafts/*.json в data/results.json.

Одну таблицу результатов могли скидывать в чат несколько раз разные участники
в разном качестве, поэтому скрипт:

1. Вычисляет дату игры для каждого черновика (приоритет: EXIF фото →
   дата с фото (распознанная моделью) → ближайшее прошедшее воскресенье
   от даты сообщения).
2. Группирует черновики по дате: черновики одной даты с пересекающимся
   составом команд считаются дублями одной игры.
3. Для дублей сверяет очки и печатает отчёт о расхождениях — их нужно
   проверить вручную по фото.
4. Нормализует названия команд через team_aliases из data/results.json
   и предупреждает о похожих, но не замердженных названиях (difflib).

По умолчанию — dry-run: только отчёт, ничего не записывается.
С флагом --apply подтверждённые игры (новые даты) дописываются в
data/results.json; уже существующие в results.json даты никогда не трогаются.
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from difflib import get_close_matches
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DRAFTS_DIR = PROJECT_ROOT / "data" / "drafts"
RESULTS_PATH = PROJECT_ROOT / "data" / "results.json"

ROUNDS_COUNT = 8
SIMILARITY_CUTOFF = 0.75


def nearest_past_sunday(d):
    """Return the nearest Sunday on or before the given date."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


def parse_iso_date(value):
    """Parse an ISO date/datetime string to a date, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:19]).date()
    except ValueError:
        return None


def game_date_for_draft(draft):
    """Compute the game date for a draft.

    Приоритет: EXIF фото → дата на фото (vision) → ближайшее прошедшее
    воскресенье от даты сообщения → дата сообщения как есть.
    """
    cand = draft.get("candidate_dates") or {}
    for value in (
        cand.get("exif"),
        (draft.get("extracted") or {}).get("game_date"),
        cand.get("nearest_sunday"),
        cand.get("message"),
    ):
        d = parse_iso_date(value)
        if d:
            return d.isoformat()
    return None


def load_drafts():
    """Load all drafts that contain actual results tables."""
    if not DRAFTS_DIR.exists():
        sys.exit(f"Ошибка: папка {DRAFTS_DIR} не найдена. "
                 "Сначала запустите vision_extract.py")
    drafts = []
    skipped = 0
    for path in sorted(DRAFTS_DIR.glob("*.json")):
        with open(path, encoding="utf-8") as f:
            draft = json.load(f)
        extracted = draft.get("extracted") or {}
        if extracted.get("is_results") is False or not extracted.get("teams"):
            skipped += 1
            continue
        draft["_file"] = path.name
        drafts.append(draft)
    print(f"Черновиков с таблицами: {len(drafts)}, не результаты/пустых: {skipped}")
    return drafts


def canonical(name, aliases):
    return aliases.get(name, aliases.get(name.strip(), name.strip()))


def team_names(draft, aliases):
    """Canonical team names of a draft."""
    return {
        canonical(t.get("name") or "", aliases)
        for t in draft["extracted"]["teams"]
        if t.get("name")
    }


def completeness(draft):
    """Score a draft by data completeness (teams count, filled round cells)."""
    teams = draft["extracted"]["teams"]
    filled = sum(
        1
        for t in teams
        for r in (t.get("rounds") or [])
        if r is not None
    )
    return (len(teams), filled)


def group_duplicates(drafts, aliases):
    """Group drafts: same date + overlapping team roster = one game.

    Returns {date: [[draft, draft, ...], ...]} — списки групп-дублей.
    """
    by_date = defaultdict(list)
    for d in drafts:
        by_date[d["_game_date"] or "unknown"].append(d)

    grouped = {}
    for date_key, date_drafts in sorted(by_date.items()):
        groups = []
        for draft in date_drafts:
            names = team_names(draft, aliases)
            target = None
            for group in groups:
                group_names = set().union(
                    *(team_names(g, aliases) for g in group)
                )
                if names & group_names:
                    target = group
                    break
            if target is not None:
                target.append(draft)
            else:
                groups.append([draft])
        grouped[date_key] = groups
    return grouped


def report_group_conflicts(group, aliases):
    """Print score mismatches between duplicate drafts of one game.

    Returns True if conflicts were found.
    """
    # Собираем по команде все версии (rounds, total, place) из всех дублей
    versions = defaultdict(list)
    for draft in group:
        for team in draft["extracted"]["teams"]:
            name = canonical(team.get("name") or "?", aliases)
            versions[name].append(
                {
                    "file": draft["_file"],
                    "rounds": team.get("rounds"),
                    "total": team.get("total"),
                    "place": team.get("place"),
                }
            )

    has_conflicts = False
    for name, items in sorted(versions.items()):
        if len(items) < len(group):
            # Команда есть не во всех дублях: возможно, вариация названия
            # (нужен alias) или модель пропустила строку таблицы
            has_conflicts = True
            files = ", ".join(i["file"] for i in items)
            print(
                f"    ВНИМАНИЕ: «{name}» есть только в {len(items)} из "
                f"{len(group)} дублей ({files}) — проверьте вариации "
                f"названия / пропущенные строки."
            )
        if len(items) < 2:
            continue
        totals = {json.dumps(i["total"]) for i in items}
        rounds = {json.dumps(i["rounds"]) for i in items}
        places = {json.dumps(i["place"]) for i in items}
        if len(totals) > 1 or len(rounds) > 1 or len(places) > 1:
            has_conflicts = True
            print(f"    КОНФЛИКТ по команде «{name}»:")
            for i in items:
                print(
                    f"      {i['file']}: место={i['place']}, "
                    f"итог={i['total']}, раунды={i['rounds']}"
                )
    return has_conflicts


def message_timestamp(draft):
    """Parse the message send timestamp from the source file name.

    Имена вида ..._00001592-PHOTO-2025-03-16-20-27-55.jpg содержат время
    отправки фото в чат. Если распарсить не удалось — datetime.min.
    """
    name = draft.get("source_file") or draft.get("_file") or ""
    m = re.search(r"PHOTO-(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})", name)
    if m:
        return datetime(*map(int, m.groups()))
    return datetime.min


def best_draft(group):
    """Pick the winning draft of a duplicate group.

    Сначала полнота (число команд, заполненные ячейки раундов): промежуточные
    таблицы после 3/6 раундов отсеиваются. Среди одинаково полных (финальных)
    дублей при расхождении очков побеждает более поздний по времени отправки:
    исправленную таблицу пересылают в чат позже.
    """
    max_score = max(completeness(d) for d in group)
    finalists = [d for d in group if completeness(d) == max_score]
    return max(finalists, key=message_timestamp)


def build_game(draft, aliases):
    """Convert a draft to a results.json game entry."""
    extracted = draft["extracted"]
    teams = []
    for t in extracted["teams"]:
        rounds = t.get("rounds") or []
        rounds = list(rounds[:ROUNDS_COUNT]) + [None] * (ROUNDS_COUNT - len(rounds))
        total = t.get("total")
        if total is None:
            known = [r for r in rounds if r is not None]
            total = sum(known) if known else None
        teams.append(
            {
                "name": canonical(t.get("name") or "?", aliases),
                "place": t.get("place"),
                "rounds": rounds,
                "total": total,
            }
        )
    teams.sort(key=lambda t: (t["place"] is None, t["place"]))
    game = {"date": draft["_game_date"], "teams": teams}
    if extracted.get("game_title"):
        game["title"] = extracted["game_title"]
    if extracted.get("venue"):
        game["venue"] = extracted["venue"]
    return game


def warn_similar_names(games, aliases):
    """Warn about similar-looking team names not merged via aliases."""
    names = sorted(
        {
            canonical(t["name"], aliases)
            for g in games
            for t in g.get("teams", [])
            if t.get("name")
        }
    )
    warned = False
    for i, name in enumerate(names):
        others = names[:i] + names[i + 1:]
        close = get_close_matches(name, others, n=3, cutoff=SIMILARITY_CUTOFF)
        # Показываем пару один раз (name < match)
        close = [m for m in close if name < m]
        if close:
            if not warned:
                print("\nПОХОЖИЕ НАЗВАНИЯ КОМАНД (возможно, одна команда — "
                      "добавьте в team_aliases):")
                warned = True
            for m in close:
                print(f"  «{name}»  ~  «{m}»")
    if not warned:
        print("\nПохожих незамердженных названий команд не найдено.")


def main():
    parser = argparse.ArgumentParser(
        description="Консолидация черновиков в data/results.json"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Дописать подтверждённые игры в results.json "
             "(без флага — только отчёт)",
    )
    args = parser.parse_args()

    if RESULTS_PATH.exists():
        with open(RESULTS_PATH, encoding="utf-8") as f:
            results = json.load(f)
    else:
        results = {"team_aliases": {}, "games": []}
    aliases = results.get("team_aliases", {})
    existing_dates = {g.get("date") for g in results.get("games", [])}

    drafts = load_drafts()
    if not drafts:
        print("Нечего консолидировать.")
        return

    for draft in drafts:
        draft["_game_date"] = game_date_for_draft(draft)

    grouped = group_duplicates(drafts, aliases)

    proposed_games = []
    conflict_dates = []
    print("\n=== ОТЧЁТ ПО ИГРАМ ===")
    for date_key, groups in grouped.items():
        for group in groups:
            files = ", ".join(d["_file"] for d in group)
            status = "уже в results.json" if date_key in existing_dates else "новая"
            print(f"\nИгра {date_key} ({status}), черновиков: {len(group)}")
            print(f"  Источники: {files}")
            if len(group) > 1:
                print(f"  Дубли обнаружены — сверяю очки:")
                if report_group_conflicts(group, aliases):
                    conflict_dates.append(date_key)
                    print("    → Требуется ручная проверка по фото!")
                else:
                    print("    Расхождений нет, дубли совпадают.")
            chosen = best_draft(group)
            game = build_game(chosen, aliases)
            n_teams = len(game["teams"])
            ts = message_timestamp(chosen)
            ts_str = ts.isoformat(" ") if ts != datetime.min else "время неизвестно"
            print(f"  Выбран черновик: {chosen['_file']} "
                  f"(команд: {n_teams}, отправлен: {ts_str} — "
                  f"самый полный, при равной полноте самый поздний)")
            if date_key not in existing_dates and date_key != "unknown":
                proposed_games.append(game)
            elif date_key == "unknown":
                print("  ВНИМАНИЕ: не удалось определить дату — "
                      "игра не будет добавлена автоматически.")

    warn_similar_names(
        proposed_games + results.get("games", []), aliases
    )

    print(f"\nИтого: новых игр к добавлению — {len(proposed_games)}, "
          f"дат с конфликтами — {len(set(conflict_dates))}")

    if not args.apply:
        print("\nDry-run: ничего не записано. "
              "Проверьте отчёт и запустите с --apply для записи.")
        return

    if not proposed_games:
        print("Новых игр нет — results.json не изменён.")
        return

    results.setdefault("games", []).extend(proposed_games)
    results["games"].sort(key=lambda g: g.get("date") or "")
    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\nЗаписано в {RESULTS_PATH}: +{len(proposed_games)} игр. "
          "Проверьте конфликты выше и запустите build_data.py.")


if __name__ == "__main__":
    main()
