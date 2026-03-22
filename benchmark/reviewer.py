"""Human review server — serves blind A/B comparison UI and collects feedback."""

from __future__ import annotations

import json
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any

from benchmark.models import EvalSuite, Feedback

_VIEWER_HTML = Path(__file__).parent / "viewer.html"


def _load_review_data(output_dir: Path) -> dict[str, Any]:
    """Load all eval outputs and gradings from the benchmark output directory."""
    suite_path = output_dir / "evals.json"
    suite = EvalSuite.from_file(suite_path)

    evals_data: list[dict[str, Any]] = []
    for case in suite.evals:
        case_dir = output_dir / f"eval-{case.id}"
        if not case_dir.is_dir():
            continue

        outputs: dict[str, dict] = {}
        gradings: dict[str, dict] = {}
        for cfg_dir in sorted(case_dir.iterdir()):
            if not cfg_dir.is_dir():
                continue
            config_name = cfg_dir.name
            out_path = cfg_dir / "output.json"
            if out_path.exists():
                outputs[config_name] = json.loads(out_path.read_text(encoding="utf-8"))
            grading_path = cfg_dir / "grading.json"
            if grading_path.exists():
                gradings[config_name] = json.loads(grading_path.read_text(encoding="utf-8"))

        evals_data.append({
            "id": case.id,
            "prompt": case.prompt,
            "assertions": case.assertions,
            "outputs": outputs,
            "gradings": gradings,
        })

    return {
        "suite_name": suite.name,
        "evals": evals_data,
    }


def _save_feedback(output_dir: Path, feedbacks: list[dict[str, Any]]) -> None:
    """Append feedback entries to feedback.json."""
    fb_path = output_dir / "feedback.json"
    existing: list[dict] = []
    if fb_path.exists():
        existing = json.loads(fb_path.read_text(encoding="utf-8"))
    existing.extend(feedbacks)
    fb_path.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def load_feedback(output_dir: str | Path) -> list[Feedback]:
    """Load feedback from disk."""
    fb_path = Path(output_dir) / "feedback.json"
    if not fb_path.exists():
        return []
    data = json.loads(fb_path.read_text(encoding="utf-8"))
    return [
        Feedback(**{k: v for k, v in fb.items() if k in Feedback.__dataclass_fields__})
        for fb in data
    ]


class _ReviewHandler(SimpleHTTPRequestHandler):
    """HTTP handler for the review UI."""

    output_dir: Path  # set by serve()
    review_data: dict[str, Any]

    def do_GET(self) -> None:
        if self.path == "/":
            content = _VIEWER_HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        elif self.path == "/api/data":
            body = json.dumps(self.review_data, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path == "/api/feedback":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            feedbacks = json.loads(raw)
            _save_feedback(self.output_dir, feedbacks)
            body = json.dumps({"message": f"Saved {len(feedbacks)} feedback entries."}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, format: str, *args: Any) -> None:
        # Quieter logging
        pass


def serve(output_dir: str | Path, port: int = 8384) -> None:
    """Launch the review UI server."""
    output_dir = Path(output_dir)
    review_data = _load_review_data(output_dir)

    # Inject state into handler class
    _ReviewHandler.output_dir = output_dir
    _ReviewHandler.review_data = review_data

    server = HTTPServer(("127.0.0.1", port), _ReviewHandler)
    print(f"Review UI: http://127.0.0.1:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: python -m benchmark.reviewer <output_dir> [port]")
        sys.exit(1)
    d = sys.argv[1]
    p = int(sys.argv[2]) if len(sys.argv) > 2 else 8384
    serve(d, p)
