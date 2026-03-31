"""Simple Flask web application for the Bernstein demo.

This file contains four intentional bugs that ``bernstein demo`` will fix:
  Bug 1 — off-by-one: get_item() uses ITEMS[n] (0-indexed) on a 1-indexed route.
  Bug 2 — missing import: request is used in /echo but not imported from flask.
  Bug 3 — wrong status code: /health returns 201 (Created) instead of 200 (OK).
  Bug 4 — broken test: test_hello_returns_200 asserts status_code == 404.
"""

from flask import Flask, jsonify  # BUG 2: 'request' is missing from this import

app = Flask(__name__)

ITEMS = ["apple", "banana", "cherry", "date"]


@app.route("/")
def hello() -> object:
    """Return a greeting."""
    return jsonify({"message": "Hello, World!", "status": "ok"})


@app.route("/items/<int:n>")
def get_item(n: int) -> object:
    """Return the nth item (1-indexed).

    BUG 1: uses ITEMS[n] (zero-indexed) — should be ITEMS[n - 1].
    Accessing n=1 returns 'banana' instead of 'apple'.
    Accessing n=4 raises IndexError.
    """
    return jsonify({"id": n, "item": ITEMS[n]})  # off-by-one


@app.route("/echo")
def echo() -> object:
    """Echo a query parameter.

    BUG 2: uses request.args but 'request' is not imported.
    Raises NameError: name 'request' is not defined.
    """
    msg = request.args.get("msg", "")  # type: ignore[name-defined]  # noqa: F821
    return jsonify({"echo": msg})


@app.route("/health")
def health() -> object:
    """Health check endpoint.

    BUG 3: returns HTTP 201 (Created) instead of 200 (OK).
    """
    return jsonify({"status": "healthy", "version": "1.0.0"}), 201  # type: ignore[return-value]


if __name__ == "__main__":
    app.run()
