"""Record actual subscription cards for ten independently specified demo meanings."""

import argparse
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CASES = [
    ("At the left we can see", "left", "Левая сторона, слева; не прошедшая форма leave."),
    ("Watch out", "Watch", "Предупреждение «осторожно», выражение watch out."),
    ("Are you hurt", "hurt", "Пострадал, получил травму; вопрос о состоянии человека."),
    ("Get up, Emo", "Get", "Встать, подняться; выражение get up."),
    ("paying attention", "paying", "Обращать внимание; выражение pay attention."),
    ("Like clockwork", "clockwork", "Как часы, слаженно и без сбоев."),
    ("ground to a pulp", "ground", "Размолот, растерт; не земля или территория."),
    ("close your eyes", "close", "Закрыть глаза; не близкий."),
    ("a sick man", "sick", "Больной человек; в контексте упрека возможно психически нездоровый."),
    ("It’s a trap", "trap", "Ловушка, западня."),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8765")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    def request(path: str, body: object | None = None) -> Any:
        payload = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            args.base_url + "/api" + path,
            data=payload,
            headers={"Content-Type": "application/json"} if payload else {},
        )
        with urllib.request.urlopen(req, timeout=40) as response:
            return json.load(response)

    summaries = request("/materials")
    identity = next(item["id"] for item in summaries if item["source"] == "demo")
    material = request("/materials/" + identity)

    def check(case: tuple[str, str, str]) -> dict[str, Any]:
        cue_fragment, surface, expected = case
        cue = next(c for c in material["cues"] if cue_fragment in c["text"])
        token = next(t for t in cue["tokens"] if t["surface"] == surface)
        result: dict[str, Any] = {
            "cue": cue["text"],
            "surface": surface,
            "expected": expected,
        }
        try:
            result["actual"] = request(
                "/cards",
                {
                    "material_id": identity,
                    "cue_id": cue["id"],
                    "token_id": token["id"],
                },
            )
            print(surface, result["actual"]["duration_ms"], "ms", flush=True)
        except Exception as error:
            result["error"] = type(error).__name__
            print(surface, result["error"], flush=True)
        return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(check, CASES))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "checked_at": datetime.now(UTC).isoformat(),
                "material_id": identity,
                "source": "Elephants Dream prepared licensed transcript",
                "concurrency": 2,
                "cases": results,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
