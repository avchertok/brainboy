#!/usr/bin/env python3
"""Сборка web/data.json из курируемого data/results.json.

Формат data/results.json:
    {
      "team_aliases": {"вариант названия": "Каноническое имя"},
      "games": [
        {"date": "YYYY-MM-DD", "title": "...", "venue": "...",
         "teams": [{"name": "...", "score": число, "place": число}]}
      ]
    }

Скрипт применяет алиасы, считает стендинг по командам и пишет web/data.json.
Если data/results.json отсутствует — создаёт пустой каркас.
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULTS_PATH = PROJECT_ROOT / "data" / "results.json"
OUTPUT_PATH = PROJECT_ROOT / "web" / "data.json"

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


def build_standings(games, aliases):
    """Aggregate per-team standings across all games."""
    teams = {}
    sorted_games = sorted(games, key=lambda g: g.get("date") or "")

    for game in sorted_games:
        for entry in game.get("teams", []):
            name = canonical_name(entry["name"], aliases)
            team = teams.setdefault(
                name,
                {
                    "name": name,
                    "games_played": 0,
                    "wins": 0,
                    "total_score": 0,
                    "best_place": None,
                    "history": [],
                },
            )
            score = entry.get("score", 0)
            place = entry.get("place")
            team["games_played"] += 1
            team["total_score"] += score
            if place == 1:
                team["wins"] += 1
            if place is not None and (
                team["best_place"] is None or place < team["best_place"]
            ):
                team["best_place"] = place
            team["history"].append(
                {"date": game.get("date"), "score": score, "place": place}
            )

    standings = []
    for team in teams.values():
        team["avg_score"] = round(team["total_score"] / team["games_played"], 1)
        standings.append(team)

    standings.sort(key=lambda t: (-t["wins"], -t["total_score"]))
    return standings, sorted_games


def apply_aliases_to_games(games, aliases):
    """Return games with canonical team names (for the web page)."""
    result = []
    for game in games:
        game = dict(game)
        game["teams"] = [
            {**t, "name": canonical_name(t["name"], aliases)}
            for t in game.get("teams", [])
        ]
        result.append(game)
    return result


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
    games = results.get("games", [])

    standings, sorted_games = build_standings(games, aliases)
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "games": apply_aliases_to_games(sorted_games, aliases),
        "standings": standings,
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Игр: {len(games)}, команд: {len(standings)}")
    print(f"Записано: {out_path}")


if __name__ == "__main__":
    main()
