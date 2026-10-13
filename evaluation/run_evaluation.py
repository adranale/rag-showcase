import argparse
import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

EVALUATION_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = EVALUATION_DIR.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
    )
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    if not 1 <= args.top_k <= 20:
        parser.error("--top-k must be between 1 and 20.")

    cases = json.loads(
        (EVALUATION_DIR / "questions.json").read_text(encoding="utf-8")
    )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = PROJECT_ROOT / "output" / "evaluation" / timestamp
    output_dir.mkdir(parents=True)

    rows = []
    details = []

    with httpx.Client(
        base_url=args.base_url.rstrip("/"),
        timeout=httpx.Timeout(240.0, connect=5.0),
    ) as client:
        client.get("/ready").raise_for_status()

        for case in cases:
            started = time.perf_counter()
            response = None

            row = {
                "id": case["id"],
                "question": case["question"],
                "expected_answer": case["expected_answer"],
                "answerable": case["answerable"],
                "http_status": "",
                "latency_seconds": "",
                "returned_answer": "",
                "insufficient_evidence": "",
                "cited_titles": "",
                "abstention_ok": "",
                "source_check_ok": "",
                "automatic_checks_ok": False,
                "error": "",
                "manual_answer_correct": "",
                "manual_citations_supported": "",
                "review_notes": "",
            }

            detail = {
                "case": case,
                "request": {
                    "question": case["question"],
                    "top_k": args.top_k,
                },
                "response": None,
            }

            try:
                response = client.post(
                    "/chat",
                    json=detail["request"],
                )
                row["http_status"] = response.status_code
                response.raise_for_status()

                data = response.json()
                detail["response"] = data

                answer = data["answer"]
                insufficient = data["insufficient_evidence"]
                citations = data["citations"]

                if not isinstance(answer, str) or not answer.strip():
                    raise ValueError("Response has no valid answer text.")

                if not isinstance(insufficient, bool):
                    raise ValueError("Invalid insufficient_evidence value.")

                if not isinstance(citations, list):
                    raise ValueError("Invalid citations value.")

                cited_titles = {
                    citation["title"]
                    for citation in citations
                }
                expected_titles = set(case["expected_source_titles"])

                abstention_ok = insufficient == (not case["answerable"])

                if case["answerable"]:
                    source_check_ok = (
                        bool(citations)
                        and expected_titles.issubset(cited_titles)
                    )
                else:
                    source_check_ok = not citations

                row.update(
                    returned_answer=answer,
                    insufficient_evidence=insufficient,
                    cited_titles="; ".join(sorted(cited_titles)),
                    abstention_ok=abstention_ok,
                    source_check_ok=source_check_ok,
                    automatic_checks_ok=(
                        abstention_ok and source_check_ok
                    ),
                )

            except (
                httpx.HTTPError,
                ValueError,
                KeyError,
                TypeError,
            ) as exc:
                row["error"] = str(exc)

                if response is not None:
                    detail["raw_response"] = response.text
                    if response.status_code >= 400:
                        row["error"] += f" | {response.text}"

            row["latency_seconds"] = round(
                time.perf_counter() - started, 3
            )
            rows.append(row)
            details.append(detail)

            status = "PASS" if row["automatic_checks_ok"] else "FAIL"
            print(
                f"{case['id']}: {status} "
                f"({row['latency_seconds']}s)"
            )

    with (output_dir / "results.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    (output_dir / "responses.json").write_text(
        json.dumps(
            {
                "run": {
                    "timestamp_utc": timestamp,
                    "base_url": args.base_url,
                    "top_k": args.top_k,
                },
                "results": details,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    passed = sum(row["automatic_checks_ok"] for row in rows)
    successful = [
        row for row in rows
        if row["http_status"] == 200 and not row["error"]
    ]

    print(f"\nAutomatic checks passed: {passed}/{len(rows)}")
    print(f"Successful responses: {len(successful)}/{len(rows)}")
    print(f"Results saved in: {output_dir}")


if __name__ == "__main__":
    main()