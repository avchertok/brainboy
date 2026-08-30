#!/usr/bin/env python3
"""Сборка web/data.json из курируемого data/results.json.

Формат data/results.json:
    {
      "team_aliases": {"вариант названия": "Каноническое имя"},
      "games": [
        {"date": "YYYY-MM-DD", "title": "...", "venue": "...",
         "season": 2025,  # необязательно: переопределение сезона
         "teams": [{"name": "...", "place": число,
                    "rounds": [8 чисел или null], "total": число}]}
      ]
    }

Сезон = необязательное поле "season" игры, а если его нет — календарный
год даты игры. Переопределённые игры помечаются в web/data.json флагом
season_override. Скрипт считает:
  - стендинги за всё время и по каждому сезону в двух системах:
    "по сумме очков" (сумма total) и "олимпийская" (1 место = 3, 2 = 2, 3 = 1);
  - историю накопительного рейтинга по датам игр (для графика) в обеих системах;
  - статистику по раундам: средние очки команд по каждому из 8 раундов
    и лидера капитанского конкурса (8-й раунд).

Всё пишется одной структурой в web/data.json.
"""

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULTS_PATH = PROJECT_ROOT / "data" / "results.json"
OUTPUT_PATH = PROJECT_ROOT / "web" / "data.json"

ROUNDS_COUNT = 8
OLYMPIC_POINTS = {1: 3, 2: 2, 3: 1}

EMPTY_RESULTS = {"team_aliases": {}, "games": []}


def load_results(path):
    """Load results.json, creating an empty skeleton if it is missing."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(EMPTY_RESULTS, f, ensure_ascii=False, indent=2)
        print(
            f"Файл {path} не найден — создан пустой каркас. "
            "Добавьте игры и запустите скрипт снова."
        )
        return dict(EMPTY_RESULTS)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def canonical_name(name, aliases):
    return aliases.get(name, name)


def season_of(game):
    """Season = explicit "season" override if set, else calendar year of the date."""
    if game.get("season") is not None:
        return str(game["season"])
    date = game.get("date") or ""
    return date[:4] if len(date) >= 4 else None


def normalize_games(games, aliases):
    """Sorted games with canonical team names, padded rounds and season."""
    result = []
    for game in sorted(games, key=lambda g: g.get("date") or ""):
        game = dict(game)
        override = game.get("season") is not None
        game["season"] = season_of(game)
        if override:
            game["season_override"] = True
        teams = []
        for t in game.get("teams", []):
            t = dict(t)
            t["name"] = canonical_name(t["name"], aliases)
            rounds = list(t.get("rounds") or [])
            t["rounds"] = rounds[:ROUNDS_COUNT] + [None] * (
                ROUNDS_COUNT - len(rounds)
            )
            if t.get("total") is None:
                known = [r for r in t["rounds"] if r is not None]
                t["total"] = sum(known) if known else 0
            teams.append(t)
        teams.sort(key=lambda t: (t.get("place") is None, t.get("place")))
        game["teams"] = teams
        result.append(game)
    return result


def olympic_for_place(place):
    return OLYMPIC_POINTS.get(place, 0)


def build_standings(games):
    """Per-team standings for a list of games (one scope)."""
    teams = {}
    for game in games:
        for entry in game["teams"]:
            team = teams.setdefault(
                entry["name"],
                {
                    "name": entry["name"],
                    "games_played": 0,
                    "wins": 0,
                    "podiums": 0,
                    "points": 0,
                    "olympic": 0,
                    "best_place": None,
                },
            )
            place = entry.get("place")
            total = entry.get("total") or 0
            team["games_played"] += 1
            team["points"] += total
            team["olympic"] += olympic_for_place(place)
            if place == 1:
                team["wins"] += 1
            if place is not None and place <= 3:
                team["podiums"] += 1
            if place is not None and (
                team["best_place"] is None or place < team["best_place"]
            ):
                team["best_place"] = place

    standings = []
    for team in teams.values():
        team["avg_points"] = round(team["points"] / team["games_played"], 1)
        standings.append(team)
    # Базовая сортировка — по сумме очков; страница пересортирует сама
    standings.sort(key=lambda t: (-t["points"], -t["olympic"], t["name"]))
    return standings


def build_history(games):
    """Cumulative rating history by game date, both systems.

    Returns {"dates": [...], "series": [{"name", "points": [...], "olympic": [...]}]}.
    До первой игры команды — null, дальше значение переносится вперёд.
    """
    dates = sorted({g["date"] for g in games if g.get("date")})
    date_index = {d: i for i, d in enumerate(dates)}
    per_team = defaultdict(lambda: {"points": [None] * len(dates),
                                    "olympic": [None] * len(dates)})

    for game in games:
        idx = date_index.get(game.get("date"))
        if idx is None:
            continue
        for entry in game["teams"]:
            slot = per_team[entry["name"]]
            gained = entry.get("total") or 0
            slot["points"][idx] = (slot["points"][idx] or 0) + gained
            slot["olympic"][idx] = (slot["olympic"][idx] or 0) + olympic_for_place(
                entry.get("place")
            )

    series = []
    for name in sorted(per_team):
        slot = per_team[name]
        for key in ("points", "olympic"):
            running = None
            values = []
            for v in slot[key]:
                if v is not None:
                    running = (running or 0) + v
                values.append(running)
            slot[key] = values
        series.append({"name": name, **slot})
    return {"dates": dates, "series": series}


def build_round_stats(games):
    """Average points per round per team + captain's round leader."""
    per_team = defaultdict(lambda: [[] for _ in range(ROUNDS_COUNT)])
    for game in games:
        for entry in game["teams"]:
            for i, value in enumerate(entry["rounds"][:ROUNDS_COUNT]):
                if value is not None:
                    per_team[entry["name"]][i].append(value)

    teams = []
    overall = [[] for _ in range(ROUNDS_COUNT)]
    for name in sorted(per_team):
        avgs = []
        for i, values in enumerate(per_team[name]):
            overall[i].extend(values)
            avgs.append(round(sum(values) / len(values), 2) if values else None)
        teams.append(
            {
                "name": name,
                "round_avgs": avgs,
                "games": max(len(v) for v in per_team[name]),
            }
        )

    overall_avgs = [
        round(sum(v) / len(v), 2) if v else None for v in overall
    ]

    # Лидер капитанского конкурса — лучший средний результат в 8-м раунде
    captain_leader = None
    for team in teams:
        avg = team["round_avgs"][ROUNDS_COUNT - 1]
        if avg is None:
            continue
        if captain_leader is None or avg > captain_leader["avg"]:
            captain_leader = {"name": team["name"], "avg": avg}

    return {
        "teams": teams,
        "overall_avgs": overall_avgs,
        "captain_leader": captain_leader,
    }


def build_scope(games):
    """Standings + history for one scope (all time or one season)."""
    return {
        "standings": build_standings(games),
        "history": build_history(games),
    }


def main():
    parser = argparse.ArgumentParser(description="Сборка web/data.json из results.json")
    parser.add_argument(
        "--results",
        default=str(RESULTS_PATH),
        help="Путь к results.json (по умолчанию data/results.json)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=str(OUTPUT_PATH),
        help="Куда писать data.json (по умолчанию web/data.json)",
    )
    args = parser.parse_args()

    results = load_results(Path(args.results))
    aliases = results.get("team_aliases", {})
    games = normalize_games(results.get("games", []), aliases)

    seasons = sorted({g["season"] for g in games if g["season"]})
    scopes = {"all": build_scope(games)}
    for season in seasons:
        scopes[season] = build_scope([g for g in games if g["season"] == season])

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rounds_count": ROUNDS_COUNT,
        "seasons": seasons,
        "games": games,
        "scopes": scopes,
        "round_stats": build_round_stats(games),
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    n_teams = len(scopes["all"]["standings"])
    print(f"Игр: {len(games)}, команд: {n_teams}, сезонов: {len(seasons)}")
    print(f"Записано: {out_path}")


if __name__ == "__main__":
    main()
